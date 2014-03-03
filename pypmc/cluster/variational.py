"""Variational clustering as described in [Bis06]_

"""

from __future__ import division, print_function
from math import log
import numpy as _np
from scipy.special import gamma as _gamma
from scipy.special import gammaln as _gammaln
from scipy.special.basic import digamma as _digamma
from ..importance_sampling.proposal import MixtureDensity, Gauss
from ..tools._doc import _inherit_docstring, _add_to_docstring
from ..tools._regularize import regularize

class GaussianInference(object):
    '''Approximate a probability density by a Gaussian mixture with a variational
    Bayes approach. The motivation, notation, and derivation is explained in
    detail in chapter 10.2 in [Bis06]_.

    .. seealso ::

        Another implementation can be found at https://github.com/jamesmcinerney/vbmm.


    :param data:

        Matrix like array; Each of the :math:`N` rows contains one
        :math:`D`-dimensional sample from the probability density to be
        approximated.

    :param components:

        Integer; :math:`K` is the number of Gaussian components in the
        approximating Gaussian mixture.

    :param weights:

        Vector-like array; The i-th of the :math:`N` entries contains the
        weight of the i-th sample in ``data``.

    All keyword arguments are processed by :py:meth:`set_variational_parameters`.

    '''
    def __init__(self, data, components, weights=None, **kwargs):
        self.data = data
        self.K = components
        self.N, self.dim = self.data.shape
        if weights is not None:
            assert weights.shape == (self.N,), \
                    "The number of samples (%s) does not match the number of weights (%s)" %(self.N, weights.shape[0])
            # normalize weights to N (not one)
            self.weights = self.N * weights / weights.sum()

            # use weighted update formulae
            self._update_N_comp = self._update_N_comp_weighted
            self._update_x_mean_comp = self._update_x_mean_comp_weighted
            self._update_S = self._update_S_weighted
            self._update_expectation_log_q_Z = self._update_expectation_log_q_Z_weighted

        self.set_variational_parameters(**kwargs)

        self._initialize_output()
        # initialize manually so subclasses can save memory
        self.expectation_gauss_exponent = _np.zeros((self.N, self.K))

        # compute expectation values for the initial parameter values
        # so a valid bound can be computed after object is initialized
        self.E_step()

    def E_step(self):
        '''Compute expectation values and summary statistics.'''

        # check ln_lambda first to catch an invalid W matrix
        # before expensive loop over samples
        self._update_expectation_det_ln_lambda()
        self._update_expectation_gauss_exponent()
        self._update_expectation_ln_pi()
        self._update_r()
        self._update_N_comp()
        self._update_x_mean_comp()
        self._update_S()

    def M_step(self):
        '''Update parameters of the Gaussian-Wishart distribution.'''

        self.nu = self.nu0 + self.N_comp
        self.alpha = self.alpha0 + self.N_comp
        self.beta = self.beta0 + self.N_comp
        self._update_m()
        self._update_W()

    def make_mixture(self):
        '''Return the mixture-density defined by the
        mode of the variational-Bayes estimate.

        '''

        # find mode of Gaussian-Wishart distribution
        # and invert to find covariance. The result
        # \Lambda_k = (\nu_k - D) W_k
        # the mode of the Gauss-Wishart exists only if \nu_k > D
        # turns out to be independent of beta.

        # The most likely value of the mean is m_k,
        # the mean parameter of the Gaussian q(\mu_k).

        # The mode of the Dirichlet exists only if \alpha_k > 1

        components = []
        weights = []
        skipped = []
        for k, W in enumerate(self.W):
            # Dirichlet mode
            # do not divide by the normalization constant because:
            #   1. this will be done automatically by the mixture contructor
            #   2. in case \alpha_k < 1 and normalization = \sum_{n=1}^{N}\(alpha_k-1) < 0
            #      the results would be nonsense but no warning would be printed
            #      because in that case \frac{\alpha_k - 1}{normalization} > 0
            pi = self.alpha[k] - 1.
            if pi <= 0:
                print("Skipped component %i because of zero weight" %k)
                skipped.append(k)
                continue

            # Gauss-Wishart mode
            if self.nu[k] <= self.dim:
                print("WARNING: Gauss-Wishart mode of component %i is not defined" %k)
                skipped.append(k)
                continue

            try:
                W = (self.nu[k] - self.dim) * W
                cov = _np.linalg.inv(W)
                components.append(Gauss(self.m[k], cov))
            except Exception as error:
                print("ERROR: Could not create component %i." %k)
                print("The error was:", repr(error) )
                skipped.append(k)
                continue

            # relative weight properly normalized
            weights.append(pi)

        if skipped:
            print("The following components have been skipped:", skipped)

        return MixtureDensity(components, weights)

    def likelihood_bound(self):
        '''Compute the lower bound on the true log marginal likelihood
        :math:`L(Q)` given the current parameter estimates.

        '''

        # todo easy to parallelize sum of independent terms
        bound  = self._update_expectation_log_p_X()
        bound += self._update_expectation_log_p_Z()
        bound += self._update_expectation_log_p_pi()
        bound += self._update_expectation_log_p_mu_lambda()
        bound -= self._update_expectation_log_q_Z()
        bound -= self._update_expectation_log_q_pi()
        bound -= self._update_expectation_log_q_mu_lambda()

        return bound

    def posterior2prior(self):
        '''Return references to posterior values of all variational parameters
        as dict.

        .. hint::
            :py:class:`.GaussianInference`\ (`..., **output`) creates a new
            instance using the inferred posterior as prior.

        '''
        return dict(alpha0=self.alpha.copy(), beta0=self.beta.copy(), nu0=self.nu.copy(),
                    m0=self.m.copy(), W0=self.W.copy(), components=self.K)

    def prior_posterior(self):
        '''Return references to prior and posterior values of all variational
        parameters as dict.

        '''

        return dict(alpha0=self.alpha0.copy(), beta0=self.beta0.copy(), m0=self.m0.copy(),
                    nu0=self.nu0.copy(), W0=self.W0.copy(), alpha=self.alpha.copy(), beta=self.beta.copy(),
                    m=self.m.copy(), nu=self.nu.copy(), W=self.W.copy(), components=self.K)

    def prune(self, threshold=1.):
        r'''Delete components with an effective number of samples
        :math:`N_k` below the threshold.

        :param threshold:

            Float; the minimum effective number of samples a component must have
            to survive.

        '''

        # nothing to do for a zero threshold
        if not threshold:
            return

        components_to_survive = _np.where(self.N_comp >= threshold)[0]
        self.K = len(components_to_survive)

        # list all vector and matrix vmembers
        vmembers = ('alpha0', 'alpha', 'beta0', 'beta', 'expectation_det_ln_lambda',
                   'expectation_ln_pi', 'N_comp', 'nu0', 'nu', 'm0', 'm', 'S', 'W0', 'inv_W0', 'W', 'x_mean_comp')
        mmembers = ('expectation_gauss_exponent', 'r')

        # shift surviving across dead components
        k_new = 0
        for k_old in components_to_survive:
            # reindex surviving components
            if k_old != k_new:
                for m in vmembers:
                    m = getattr(self, m)
                    m[k_new] = m[k_old]
                for m in mmembers:
                    m = getattr(self, m)
                    m[:, k_new] = m[:, k_old]

            k_new += 1

        # cut the unneccessary part of the data
        for m in vmembers:
            setattr(self, m, getattr(self, m)[:self.K])
        for m in mmembers:
            setattr(self, m, getattr(self, m)[:, :self.K])

        # recreate consistent expectation values
        self.E_step()

    def run(self, iterations=25, prune=1., rel_tol=1e-4, abs_tol=1e-3, verbose=False):
        r'''Run variational-Bayes parameter updates and check for convergence
        using the change of the log likelihood bound of the current and the last
        step. Convergence is not declared if the number of components changed,
        or if the bound decreased. For the standard algorithm, the bound must
        increase, but for modifications, this useful property may not hold for
        all parameter values.

        Return the number of iterations at convergence, or None.

        :param iterations:
            Maximum number of updates.

        :param prune:
            Call :py:meth:`prune` after each update; i.e., remove components
            whose associated effective number of samples is below the
            threshold. Set `prune=0` to deactivate.
            Default: 1 (effective samples).

        :param rel_tol:
            Relative tolerance :math:`\epsilon`. If two consecutive values of
            the log likelihood bound, :math:`L_t, L_{t-1}`, are close, declare
            convergence. More precisely, check that

            .. math::
                \left\| \frac{L_t - L_{t-1}}{L_t} \right\| < \epsilon .

        :param abs_tol:
            Absolute tolerance :math:`\epsilon_{a}`. If the current bound
            :math:`L_t` is close to zero, (:math:`L_t < \epsilon_{a}`), declare
            convergence if

            .. math::
                \| L_t - L_{t-1} \| < \epsilon_a .

        :param verbose:
            Output status information after each update.

        '''
        old_K = None
        for i in range(1, iterations + 1):
            # recompute bound in 1st step or if components were removed
            if self.K == old_K:
                old_bound = bound
            else:
                old_bound = self.likelihood_bound()
                if verbose:
                    print('New bound=%g, K=%d, N_k=%s' % (old_bound, self.K, self.N_comp))

            self.update()
            bound = self.likelihood_bound()

            if verbose:
                print('After update %d: bound=%g, K=%d, N_k=%s' % (i, bound, self.K, self.N_comp))

            if bound < old_bound:
                print('WARNING: bound decreased from %g to %g' % (old_bound, bound))

             # exact convergence
            if bound == old_bound:
                return i
            # approximate convergence
            # but only if bound increased
            diff = bound - old_bound
            if diff > 0:
                # handle case when bound is close to 0
                if abs(bound) < abs_tol:
                    if abs(diff) < abs_tol:
                        return i
                else:
                    if abs(diff / bound) < rel_tol:
                        return i

            # save K *before* pruning
            old_K = self.K
            self.prune(prune)
        # not converged
        return None

    def set_variational_parameters(self, *args, **kwargs):
        r'''Reset the parameters to the submitted values or default.

        Use this function to set the prior value (indicated by the
        subscript `0` as in :math:`\alpha_0`) or the initial value
        (e.g., :math:`\alpha`) used in the iterative procedure to find
        the posterior value of the variational distribution.

        Every parameter can be set in two ways:

        1. It is specified for only one component, then it is copied
        to all other components.

        2. It is specified separately for each component as a
        :math:`K` vector.

        The prior and posterior variational distributions of
        :math:`\boldsymbol{\mu}` and :math:`\boldsymbol{\Lambda}` for
        each component are given by

        .. math::

            q(\boldsymbol{\mu}, \boldsymbol{\Lambda}) =
            q(\boldsymbol{\mu}|\boldsymbol{\Lambda}) q(\boldsymbol{\Lambda}) =
            \prod_{k=1}^K
              \mathcal{N}(\boldsymbol{\mu}_k|\boldsymbol{m_k},(\beta_k\boldsymbol{\Lambda}_k)^{-1})
              \mathcal{W}(\boldsymbol{\Lambda}_k|\boldsymbol{W_k}, \nu_k),

        where :math:`\mathcal{N}` denotes a Gaussian and
        :math:`\mathcal{W}` a Wishart distribution. The weights
        :math:`\boldsymbol{\pi}` follow a Dirichlet distribution

        .. math::
                q(\boldsymbol{\pi}) = Dir(\boldsymbol{\pi}|\boldsymbol{\alpha}).

        .. warning ::

            This function may delete results obtained by ``self.update``.

        .. note::

            For good results, it is strongly recommended to NOT initialize
            ``m`` to values close to the bulk of the target distribution.
            For all other parameters, consult chapter 10 in [Bis06]_ when
            considering to modify the defaults.

        :param alpha0, alpha:

            Float or :math:`K` vector; parameter of the mixing
            coefficients' probability distribution (prior:
            :math:`\alpha_0`, posterior initial value: :math:`\alpha`).

            .. math::
                \alpha_i > 0, i=1 \dots K.

            A scalar is promoted to a :math:`K` vector as

            .. math::
                \boldsymbol{\alpha} = (\alpha,\dots,\alpha),

            but a `K` vector is accepted, too.

            Default:

            .. math::
                \alpha = 10^{-5}.

        :param beta0, beta:

            Float or :math:`K` vector; :math:`\beta` parameter of
            the probability distribution of :math:`\boldsymbol{\mu}`
            and :math:`\boldsymbol{\Lambda}`. The same restrictions
            as for ``alpha`` apply. Default:

            .. math::
                \beta_0 = 10^{-5}.

        :param nu0, nu:

            Float or :math:`K` vector; :math:`\nu` is the minimum
            value of the number of degrees of freedom of the Wishart
            distribution of :math:`\boldsymbol{\Lambda}`.  To avoid a
            divergence at the origin, it is required that

            .. math::
                \nu_0 \geq D + 1.

            The same restrictions as for ``alpha`` apply.

            Default:

            .. math::
                \nu_0 = D+1.

        :param m0, m:

            :math:`D` vector or :math:`K \times D` matrix; mean
            parameter for the Gaussian
            :math:`q(\boldsymbol{\mu_k}|\boldsymbol{m_k}, \beta_k
            \Lambda_k)`.

            Default:

            For the prior of each component:

            .. math::
                \boldsymbol{m}_0 = (0,\dots,0)

            For initial value of the posterior,
            :math:`\boldsymbol{m}`: the sequence of :math:`K \times D`
            equally spaced values in [-1,1] reshaped to :math:`K
            \times D` dimensions.

            .. warning:: If all :math:`\boldsymbol{m}_k` are identical
                initially, they may remain identical. It is advisable
                to randomly scatter them in order to avoid singular
                behavior.

        :param W0, W:

            :math:`D \times D` or :math:`K \times D \times D`
            matrix-like array; :math:`\boldsymbol{W}` is a symmetric
            positive-definite matrix used in the Wishart distribution.
            Default: identity matrix in :math:`D` dimensions for every
            component.

        '''
        if args: raise TypeError('keyword args only')

        self.alpha0 = kwargs.pop('alpha0', 1e-5)
        if not _np.iterable(self.alpha0):
            self.alpha0 =  self.alpha0 * _np.ones(self.K)
        else:
            self.alpha0 = _np.array(self.alpha0)
        self._check_K_vector('alpha0')
        self.alpha = kwargs.pop('alpha', _np.ones(self.K) * self.alpha0)
        self._check_K_vector('alpha')
        self.alpha = _np.array(self.alpha)

        # in the limit beta --> 0: uniform prior
        self.beta0 = kwargs.pop('beta0', 1e-5)
        if not _np.iterable(self.beta0):
            self.beta0 =  self.beta0 * _np.ones(self.K)
        else:
            self.beta0 = _np.array(self.beta0)
        self._check_K_vector('beta0')
        self.beta = kwargs.pop('beta', _np.ones(self.K) * self.beta0)
        self._check_K_vector('beta')
        self.beta = _np.array(self.beta)

        # smallest possible nu such that the Wishart pdf does not diverge at 0 is self.dim + 1
        # smallest possible nu such that the Gauss-Wishart pdf does not diverge is self.dim
        # allowed values: nu > self.dim - 1
        nu_min = self.dim - 1.
        self.nu0 = kwargs.pop('nu0', nu_min + 1e-5)
        if not _np.iterable(self.nu0):
            self.nu0 = self.nu0 * _np.ones(self.K)
        else:
            self.nu0 = _np.array(self.nu0)
        self._check_K_vector('nu0', min=nu_min)
        self.nu = kwargs.pop('nu', self.nu0 * _np.ones(self.K))
        self._check_K_vector('nu', min=nu_min)
        self.nu = _np.array(self.nu)

        self.m0 = _np.array( kwargs.pop('m0', _np.zeros(self.dim)) )
        if len(self.m0) == self.dim:
            # vector or matrix?
            if len(self.m0.shape) == 1:
                self.m0 = _np.vstack(tuple([self.m0] * self.K))

        # If the initial means are identical, the K remain identical in all updates.
        self.m      = kwargs.pop('m'     , None)
        if self.m is None:
            self.m = _np.linspace(-1.,1., self.K*self.dim).reshape((self.K, self.dim))
        else:
            self.m = _np.array(self.m)
        for name in ('m0', 'm'):
            if getattr(self, name).shape != (self.K, self.dim):
                raise ValueError('Shape of %s %s does not match (K,d)=%s' % (name, self.m.shape, (self.K, self.dim)))

        # covariance matrix; unit matrix <--> unknown correlation
        self.W0     = kwargs.pop('W0', None)
        if self.W0 is None:
            self.W0     = _np.eye(self.dim)
            self.inv_W0 = self.W0.copy()
        elif self.W0.shape == (self.dim, self.dim):
            self.W0     = _np.array(self.W0)
            self.inv_W0 = _np.linalg.inv(self.W0)
        # handle both above cases
        if self.W0.shape == (self.dim, self.dim):
            self.W0 = _np.array([self.W0] * self.K)
            self.inv_W0 = _np.array([self.inv_W0] * self.K)
        # full sequence of matrices given
        elif self.W0.shape == (self.K, self.dim, self.dim):
            self.inv_W0 = _np.array([_np.linalg.inv(W0) for W0 in self.W0])
        else:
            raise ValueError('W0 is neither None, nor a %s array, nor a %s array.' % ((self.dim, self.dim), (self.K, self.dim, self.dim)))
        self.W      = kwargs.pop('W', self.W0.copy())
        if self.W.shape != (self.K, self.dim, self.dim):
            raise ValueError('Shape of W %s does not match (K, d, d)=%s' % (self.W.shape, (self.K, self.dim, self.dim)))

        if kwargs: raise TypeError('unexpected keyword(s): ' + str(kwargs.keys()))

    def update(self):
        '''Recalculate the parameters (M step) and expectation values (E step)
        using the update equations.

        '''

        self.M_step()
        self.E_step()

    def _check_K_vector(self, name, min=0.0):
        v = getattr(self, name)
        if len(v.shape) != 1:
            raise ValueError('%s is not a vector but has shape %s' % (name, v.shape))
        if len(v) != self.K:
            raise ValueError('len(%s)=%d does not match K=%d' % (name, len(v), self.K))
        if not (v > min).all():
            raise ValueError('All elements of %s must exceed %g. %s=%s' % (name, min, name, v))

    def _initialize_output(self):
        '''Create all variables needed for the iteration in ``self.update``'''
        self.x_mean_comp = _np.zeros((self.K, self.dim))
        self.N_comp = _np.zeros(self.K)
        self.S = _np.empty_like(self.W)

    def _update_log_rho(self):
        # (10.46)

        # writing it out improves numerical precision from 1e-13 to machine precision

        # (NxK) matrix
        self.log_rho  = -0.5 * self.expectation_gauss_exponent
        # adding a K vector to (NxK) matrix adds to every row. That's what we want.
        self.log_rho += self.expectation_ln_pi
        self.log_rho += 0.5 * self.expectation_det_ln_lambda
        # adding a scalar to every element
        self.log_rho -= 0.5 * self.dim * log(2. * _np.pi)

    def _update_m(self):
        # (10.61)

        for k in range(self.K):
            self.m[k] = 1. / self.beta[k] * (self.beta0[k] * self.m0[k] + self.N_comp[k] * self.x_mean_comp[k])

    def _update_N_comp_weighted(self):
        # modified (10.51)

        _np.einsum('n,nk->k', self.weights, self.r, out=self.N_comp)
        self.inv_N_comp = 1. / regularize(self.N_comp)

    def _update_N_comp(self):
        # (10.51)

        _np.einsum('nk->k', self.r, out=self.N_comp)
        self.inv_N_comp = 1. / regularize(self.N_comp)

    def _update_r(self):
        # (10.49)

        self._update_log_rho()

        # rescale log to avoid division by zero:
        # find largest log for fixed comp. k
        # and subtract it s.t. largest value at 0 (or 1 on linear scale)
        rho = self.log_rho - self.log_rho.max(axis=1).reshape((len(self.log_rho), 1))
        rho = _np.exp(rho)

        # compute normalization for each comp. k
        normalization_rho = rho.sum(axis=1).reshape((len(rho), 1))

        # in the division, the extra scale factor drops out automagically
        self.r = rho / normalization_rho

        # avoid overflows and nans when taking the log of 0
        regularize(self.r)

    def _update_expectation_det_ln_lambda(self):
        # (10.65)

        # negative determinants from improper matrices trigger ValueError on some machines only;
        # so test explicitly
        dets = _np.array([_np.linalg.det(W) for W in self.W])
        assert (dets > 0).all(), 'Some precision matrix is not positive definite in\n %s' % self.W

        res = _np.zeros_like(self.nu)
        tmp = _np.zeros_like(self.nu)
        for i in range(1, self.dim + 1):
            tmp[:] = self.nu
            tmp += 1. - i
            tmp *= 0.5
            # digamma aware of vector input
            res += _digamma(tmp)

        res += self.dim * log(2.)
        res += _np.log(dets)

        self.expectation_det_ln_lambda = res

    def _update_expectation_gauss_exponent(self):
        # (10.64)

        tmp = _np.zeros_like(self.data[0])

        for k in range(self.K):
            for n in range(self.N):
                tmp[:] = self.data[n]
                tmp   -= self.m[k]
                self.expectation_gauss_exponent[n,k] = self.dim / self.beta[k] + self.nu[k] * tmp.dot(self.W[k]).dot(tmp)

    def _update_expectation_ln_pi(self):
        # (10.66)

        self.expectation_ln_pi = _digamma(self.alpha) - _digamma(self.alpha.sum())

    def _update_x_mean_comp_weighted(self):
        # modified (10.52)

        _np.einsum('k,n,nk,ni->ki', self.inv_N_comp, self.weights, self.r, self.data, out=self.x_mean_comp)

    def _update_x_mean_comp(self):
        # (10.52)

        _np.einsum('k,nk,ni->ki', self.inv_N_comp, self.r, self.data, out=self.x_mean_comp)

    def _update_S_weighted(self):
        # modified (10.53)

        # temp vector and matrix to store outer product
        tmpv = _np.empty_like(self.data[0])
        outer = _np.empty_like(self.S[0])

        # use outer product to guarantee a positive definite symmetric S
        # expanding it into four terms, then using einsum failed numerically for large N
        for k in range(self.K):
            self.S[k,:,:] = 0
            for n, x in enumerate(self.data):
                tmpv[:] = x
                tmpv -= self.x_mean_comp[k]
                _np.einsum('i,j', tmpv, tmpv, out=outer)
                outer *= self.r[n,k] * self.weights[n]
                self.S[k] += outer
            self.S[k] *= self.inv_N_comp[k]

    def _update_S(self):
        # (10.53)

        # temp vector and matrix to store outer product
        tmpv = _np.empty_like(self.data[0])
        outer = _np.empty_like(self.S[0])

        # use outer product to guarantee a positive definite symmetric S
        # expanding it into four terms, then using einsum failed numerically for large N
        for k in range(self.K):
            self.S[k,:,:] = 0
            for n, x in enumerate(self.data):
                tmpv[:] = x
                tmpv -= self.x_mean_comp[k]
                _np.einsum('i,j', tmpv, tmpv, out=outer)
                outer *= self.r[n,k]
                self.S[k] += outer
            self.S[k] *= self.inv_N_comp[k]

    def _update_W(self):
        # (10.62)

        # change order of operations to minimize copying
        for k in range(self.K):
            tmp = self.x_mean_comp[k] - self.m0[k]
            cov = _np.outer(tmp, tmp)
            cov *= self.beta0[k] / (self.beta0[k] + self.N_comp[k])
            cov += self.S[k]
            cov *= self.N_comp[k]
            cov += self.inv_W0[k]
            self.W[k] = _np.linalg.inv(cov)

    def _update_expectation_log_p_X(self):
        # (10.71)

        self._expectation_log_p_X = 0.
        for k in range(self.K):
            res = 0.
            tmp = self.x_mean_comp[k] - self.m[k]
            res += self.expectation_det_ln_lambda[k]
            res -= self.dim / self.beta[k]
            res -= self.nu[k] * (_np.trace(self.S[k].dot(self.W[k])) + tmp.dot(self.W[k]).dot(tmp))
            res -= self.dim * log(2 * _np.pi)
            res *= self.N_comp[k]
            self._expectation_log_p_X += res

        self._expectation_log_p_X /= 2.0
        return self._expectation_log_p_X

    def _update_expectation_log_p_Z(self):
        # (10.72)

        # simplify to include sum over k: N_k = sum_n r_{nk}

        # contract all indices, no broadcasting
        self._expectation_log_p_Z = _np.einsum('k,k', self.N_comp, self.expectation_ln_pi)
        return self._expectation_log_p_Z

    def _update_expectation_log_p_pi(self):
        # (10.73)

        self._expectation_log_p_pi = Dirichlet_log_C(self.alpha0)
        self._expectation_log_p_pi += _np.einsum('k,k', self.alpha0 - 1, self.expectation_ln_pi)
        return self._expectation_log_p_pi

    def _update_expectation_log_p_mu_lambda(self):
        # (10.74)

        res = 0
        for k in range(self.K):
            tmp = self.m[k] - self.m0[k]
            res += self.dim * log(self.beta0[k] / (2. * _np.pi))
            res += self.expectation_det_ln_lambda[k] - self.dim * self.beta0[k] / self.beta[k] \
                   - self.beta0[k] * self.nu[k] * tmp.dot(self.W[k]).dot(tmp)

            # 2nd part: Wishart normalization
            res +=  2 * Wishart_log_B(self.W0[k], self.nu0[k])

            # 3rd part
            res += (self.nu0[k] - self.dim - 1) * self.expectation_det_ln_lambda[k]

            # 4th part: traces
            res -= self.nu[k] * _np.trace(self.inv_W0[k].dot(self.W[k]))

        self._expectation_log_p_mu_lambda = 0.5 * res
        return self._expectation_log_p_mu_lambda

    def _update_expectation_log_q_Z_weighted(self):
        # modified (10.75)

        self._expectation_log_q_Z = _np.einsum('n,nk,nk', self.weights, self.r, _np.log(self.r))
        return self._expectation_log_q_Z

    def _update_expectation_log_q_Z(self):
        # (10.75)

        self._expectation_log_q_Z = _np.einsum('nk,nk', self.r, _np.log(self.r))
        return self._expectation_log_q_Z

    def _update_expectation_log_q_pi(self):
        # (10.76)

        self._expectation_log_q_pi = _np.einsum('k,k', self.alpha - 1, self.expectation_ln_pi) + Dirichlet_log_C(self.alpha)
        return self._expectation_log_q_pi

    def _update_expectation_log_q_mu_lambda(self):
        # (10.77)

        # pull constant out of loop
        res = -0.5 * self.K * self.dim

        for k in range(self.K):
            res += 0.5 * (self.expectation_det_ln_lambda[k] + self.dim * log(self.beta[k] / (2 * _np.pi)))
            # Wishart entropy
            res -= Wishart_H(self.W[k], self.nu[k])

        self._expectation_log_q_mu_lambda = res
        return self._expectation_log_q_mu_lambda

class VBMerge(GaussianInference):
    '''Parsimonious reduction of Gaussian mixture models with a
    variational-Bayes approach [BGP10]_.

    The idea is to reduce the number of components of an overly complex Gaussian
    mixture while retaining an accurate description. The original samples are
    not required, hence it much faster compared to standard variational Bayes.
    The great advantage compared to hierarchical clustering is that the number
    of output components is chosen automatically. One starts with (too) many
    components, updates, and removes those components with vanishing weight
    using  ``prune()``. All the methods the typical user wants to call are taken
    over from and documented in :py:class:`GaussianInference`.

    :param input_mixture:

        MixtureDensity with Gauss components, the input to be compressed.

    :param N:

        The number of (virtual) input samples that the ``input_mixture`` is
        based on. For example, if ``input_mixture`` was fitted to 1000 samples,
        set ``N`` to 1000.

    :param components:

        Integer; the maximum number of output components.

    :param initial_guess:

        MixtureDensity with Gauss components, optional; the starting point
        for the optimization. If provided, its number of components defines
        the maximum possible and the parameter ``components`` is ignored.


    All other keyword arguments are documented in
    :py:meth:`GaussianInference.set_variational_parameters`.

    .. seealso::

        :py:class:`pypmc.importance_sampling.proposal.MixtureDensity`

        :py:class:`pypmc.importance_sampling.proposal.Gauss`

    '''

    def __init__(self, input_mixture, N, components=None, initial_guess=None, **kwargs):
        # don't copy input_mixture, we won't update it
        self.input = input_mixture

        # number of input components
        self.L = len(input_mixture.components)

        # input means
        self.mu = _np.array([c.mu for c in self.input.components])

        if initial_guess is not None:
            self.K = len(initial_guess.components)
        elif components is not None:
            self.K = components
        else:
            raise ValueError('Specify either `components` or `initial_guess` to set the initial values')

        self.dim = len(input_mixture[0][0].mu)
        self.N = N

        # effective number of samples per input component
        # in [BGP10], that's N \cdot \omega' (vector!)
        self.Nomega = N * self.input.weights

        self.set_variational_parameters(**kwargs)

        self._initialize_output()

        # take mean and covariances from initial guess
        if initial_guess is not None:

            self.W = _np.array([c.inv_sigma / (self.nu[k] - self.dim) for k,c in enumerate(initial_guess.components)])

            # copy over the means
            self.m = _np.array([c.mu for c in initial_guess.components])

            self.alpha = N * _np.array(initial_guess.weights)

        self.E_step()

    def _initialize_output(self):
        GaussianInference._initialize_output(self)
        self.expectation_gauss_exponent = _np.zeros((self.L, self.K))

    def _update_expectation_gauss_exponent(self):
        # after (40) in [BGP10]
        for k, W in enumerate(self.W):
            for l, comp in enumerate(self.input.components):
                tmp = comp.mu - self.m[k]
                self.expectation_gauss_exponent[l,k] = self.dim / self.beta[k] + self.nu[k] * \
                                                       (_np.trace(W.dot(comp.sigma)) + tmp.dot(W).dot(tmp))

    def _update_log_rho(self):
        # (40) in [BGP10]
        # first line: compute k vector
        tmp_k  = 2 * self.expectation_ln_pi
        tmp_k += self.expectation_det_ln_lambda
        tmp_k -= self.dim * _np.log(2 * _np.pi)

        # turn into lk matrix
        self.log_rho = _np.einsum('l,k->lk', self.Nomega, tmp_k)

        # add second line
        self.log_rho -= _np.einsum('l,lk->lk', self.Nomega, self.expectation_gauss_exponent)

        self.log_rho /= 2.0

    def _update_N_comp(self):
        # (41)
        _np.einsum('l,lk', self.Nomega, self.r, out=self.N_comp)
        regularize(self.N_comp)
        self.inv_N_comp = 1. / self.N_comp

    def _update_x_mean_comp(self):
        # (42)
        _np.einsum('k,l,lk,li->ki', self.inv_N_comp, self.Nomega, self.r, self.mu, out=self.x_mean_comp)

    def _update_S(self):
        # combine (43) and (44), since only ever need sum of S and C

        for k in range(self.K):
            self.S[k,:] = 0.0
            for l in range(self.L):
                tmp        = self.mu[l] - self.x_mean_comp[k]
                self.S[k] += self.Nomega[l] * self.r[l,k] * (_np.outer(tmp, tmp) + self.input.components[l].sigma)

            self.S[k] *= self.inv_N_comp[k]

# todo move Wishart stuff to separate class, file?
# todo doesn't check that nu > D - 1
def Wishart_log_B(W, nu, det=None):
    '''Compute first part of a Wishart distribution's normalization,
    (B.79) of [Bis06]_, on the log scale.

    :param W:

        Covariance matrix of a Wishart distribution.

    :param nu:

        Degrees of freedom of a Wishart distribution.

    :param det:

        The determinant of ``W``, :math:`|W|`. If `None`, recompute it.

    '''

    if det is None:
        det = _np.linalg.det(W)

    log_B = -0.5 * nu * log(det)
    log_B -= 0.5 * nu * len(W) * log(2)
    log_B -= 0.25 * len(W) * (len(W) - 1) * log(_np.pi)
    for i in range(1, len(W) + 1):
        log_B -= _gammaln(0.5 * (nu + 1 - i))

    return log_B

def Wishart_expect_log_lambda(W, nu):
    ''' :math:`E[\log |\Lambda|]`, (B.81) of [Bis06]_ .'''
    result = 0
    for i in range(1, len(W) + 1):
        result += _digamma(0.5 * (nu + 1 - i))
    result += len(W) * log(2.)
    result += log(_np.linalg.det(W))
    return result

def Wishart_H(W, nu):
    '''Entropy of the Wishart distribution, (B.82) of [Bis06]_ .'''

    log_B = Wishart_log_B(W, nu)

    expect_log_lambda = Wishart_expect_log_lambda(W, nu)

    # dimension
    D = len(W)

    return -log_B - 0.5 * (nu - D - 1) * expect_log_lambda + 0.5 * nu * D

def Dirichlet_log_C(alpha):
    '''Compute normalization constant of Dirichlet distribution on
    log scale, (B.23) of [Bis06]_ .

    '''

    # compute gamma functions on log scale to avoid overflows
    log_C = _gammaln(_np.einsum('k->', alpha))
    for alpha_k in alpha:
        log_C -= _gammaln(alpha_k)

    return log_C
