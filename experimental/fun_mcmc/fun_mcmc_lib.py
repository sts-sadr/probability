# Copyright 2018 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Functional MCMC: A functional API for creating new Markov Chains.

The core convention of this API is that transition operators have the following
form:

```
transition_operator(state...) -> (new_state..., extra_outputs)
```

Where 'x...', reresents one or more values. This operator can then be called
recursively as follows:

```
state = ...
while not_done:
  state, extra = transition_operator(*state)
```

`state` is allowed to be partially specified (i.e. have `None` elements), which
the transition operator must impute when it returns the new state.
"""

from __future__ import absolute_import
from __future__ import division
# [internal] enable type annotations
from __future__ import print_function

import collections

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from typing import Any, Callable, Mapping, Tuple, Union, Sequence
from tensorflow.python.util import nest  # pylint: disable=g-direct-tensorflow-import

tfb = tfp.bijectors
mcmc_util = tfp.mcmc.internal.util

__all__ = [
    'blanes_3_stage_step',
    'blanes_4_stage_step',
    'call_and_grads',
    'call_fn',
    'hamiltonian_monte_carlo',
    'HamiltonianMonteCarloExtra',
    'HamiltonianMonteCarloState',
    'IntegratorStepState',
    'leapfrog_step',
    'maybe_broadcast_structure',
    'metropolis_hastings_step',
    'PotentialFn',
    'ruth4_step',
    'sign_adaptation',
    'State',
    'symmetric_spliting_integrator_step',
    'trace',
    'transform_log_prob_fn',
    'TransitionOperator',
]

AnyTensor = Union[tf.Tensor, np.ndarray, np.generic]
IntTensor = Union[int, tf.Tensor, np.ndarray, np.integer]
FloatTensor = Union[float, tf.Tensor, np.ndarray, np.floating]
# TODO(b/109648354): Correctly represent the recursive nature of this type.
TensorNest = Union[AnyTensor, Sequence[AnyTensor], Mapping[Any, AnyTensor]]
BijectorNest = Union[tfb.Bijector, Sequence[tfb.Bijector], Mapping[Any, tfb
                                                                   .Bijector]]
FloatNest = Union[FloatTensor, Sequence[FloatTensor], Mapping[Any, FloatTensor]]
State = TensorNest  # pylint: disable=invalid-name
TransitionOperator = Union[Callable[[TensorNest], Tuple[State, TensorNest]],
                           Callable[..., Tuple[State, TensorNest]]]
PotentialFn = Union[Callable[[TensorNest], Tuple[tf.Tensor, TensorNest]],
                    Callable[..., Tuple[tf.Tensor, TensorNest]]]


def trace(state: State, fn: TransitionOperator, num_steps: IntTensor,
          trace_fn: Callable[[State, TensorNest], TensorNest]
         ) -> Tuple[State, TensorNest]:
  """`TransitionOperator` that runs `fn` repeatedly and traces its outputs.

  Args:
    state: A nest of `Tensor`s or None.
    fn: A `TransitionOperator`.
    num_steps: Number of steps to run the function for. Must be greater than 1.
    trace_fn: Callable that the unpacked outputs of `fn` and returns a nest of
      `Tensor`s. These will be stacked and returned.

  Returns:
    state: The final state returned by `fn`.
    traces: Stacked outputs of `trace_fn`.
  """

  def fn_wrapper(args, _):
    return tf.nest.map_structure(tf.convert_to_tensor, call_fn(fn, args[0]))

  def trace_fn_wrapper(args):
    return tf.nest.map_structure(tf.convert_to_tensor, call_fn(trace_fn, args))

  state = call_fn(fn, state)
  first_trace = trace_fn_wrapper(state)

  state, full_trace = mcmc_util.trace_scan(
      fn_wrapper, state, tf.ones(num_steps - 1), trace_fn=trace_fn_wrapper)

  prepend = lambda x, y: tf.concat(  # pylint: disable=g-long-lambda
      [tf.convert_to_tensor(value=x)[tf.newaxis], y], 0)

  return state, tf.nest.map_structure(prepend, first_trace, full_trace)


def call_fn(fn: TransitionOperator, args: Union[Tuple[Any], Any]) -> Any:
  """Calls a transition operator with args, unpacking args if its a sequence.

  Args:
    fn: A `TransitionOperator`.
    args: Arguments to `fn`

  Returns:
    ret: Return value of `fn`.
  """

  if isinstance(args, (list, tuple)) and not mcmc_util.is_namedtuple_like(args):
    args = args  # type: Tuple[Any]
    return fn(*args)
  else:
    return fn(args)


def call_and_grads(fn: TransitionOperator, args: Union[Tuple[Any], Any]
                  ) -> Tuple[tf.Tensor, TensorNest, TensorNest]:
  """Calls `fn` and returns the gradients with respect to `fn`'s first output.

  Args:
    fn: A `TransitionOperator`.
    args: Arguments to `fn`

  Returns:
    ret: First output of `fn`.
    extra: Second output of `fn`.
    grads: Gradients of `ret` with respect to `args`.
  """
  with tf.GradientTape() as tape:
    tape.watch(args)
    ret, extra = call_fn(fn, args)
  grads = tape.gradient(ret, args)
  return ret, extra, grads


def maybe_broadcast_structure(from_structure: Any, to_structure: Any) -> Any:
  """Maybe broadcasts `from_structure` to `to_structure`.

  If `from_structure` is a singleton, it is tiled to match the structure of
  `to_structure`. Note that the elements in `from_structure` are not copied if
  this tiling occurs.

  Args:
    from_structure: A structure.
    to_structure: A structure.

  Returns:
    new_from_structure: Same structure as `to_structure`.
  """
  flat_from = tf.nest.flatten(from_structure)
  flat_to = tf.nest.flatten(to_structure)
  if len(flat_from) == 1:
    flat_from *= len(flat_to)
  return tf.nest.pack_sequence_as(to_structure, flat_from)


def transform_log_prob_fn(log_prob_fn: PotentialFn,
                          bijector: BijectorNest,
                          init_state: State = None
                         ) -> Any:
  """Transforms a log-prob function using a bijector.

  This takes a log-prob function and creates a new log-prob function that now
  takes takes state in the domain of the bijector, forward transforms that state
  and calls the original log-prob function. It then returns the log-probability
  that correctly accounts for this transformation.

  The forward-transformed state is pre-pended to the original log-prob
  function's extra returns and returned as the new extra return.

  For convenience you can also pass the initial state (in the original space),
  and this function will return the inverse transformed as the 2nd return value.
  You'd use this to initialize MCMC operators that operate in the transformed
  space.

  Args:
    log_prob_fn: Log prob fn.
    bijector: Bijector(s), must be of the same structure as the `log_prob_fn`
      inputs.
    init_state: Initial state, in the original space.

  Returns:
    transformed_log_prob_fn: Transformed log prob fn.
    transformed_init_state: If `init_state` is provided. Initial state in the
      transformed space.
  """

  def wrapper(*args):
    """Transformed wrapper."""
    bijector_ = bijector

    args = tf.nest.map_structure(lambda x: 0. + x, args)
    if len(args) == 1:
      args = args[0]
    elif isinstance(bijector_, list):
      bijector_ = tuple(bijector_)

    original_space_args = tf.nest.map_structure(lambda b, x: b.forward(x),
                                                bijector_, args)
    original_space_args = original_space_args  # type: Tuple[Any]
    original_space_log_prob, extra = call_fn(log_prob_fn, original_space_args)
    event_ndims = tf.nest.map_structure(
        lambda x: tf.rank(x) - tf.rank(original_space_log_prob), args)

    return original_space_log_prob + sum(
        tf.nest.flatten(
            tf.nest.map_structure(
                lambda b, x, e: b.forward_log_det_jacobian(x, event_ndims=e),
                bijector_, args, event_ndims))), [original_space_args, extra]

  if init_state is None:
    return wrapper
  else:
    return wrapper, tf.nest.map_structure(lambda b, s: b.inverse(s), bijector,
                                          init_state)


IntegratorStepState = collections.namedtuple('IntegratorStepState',
                                             'state, state_grads, momentum')
IntegratorStepExtras = collections.namedtuple(
    'IntegratorStepExtras', 'target_log_prob, state_extra, '
    'kinetic_energy, kinetic_energy_extra')


def symmetric_spliting_integrator_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
    coefficients: Sequence[FloatTensor],
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Symmetric symplectic integrator `TransitionOperator`.

  This implementation is based on Hamiltonian splitting, with the splits
  weighted by coefficients. We assume a symmetric integrator, so only the first
  `N / 2 + 1` coefficients need be specified. We always update the momentum
  first. See [1] for an overview of the method.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.
    coefficients: First N / 2 + 1 coefficients.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  if len(coefficients) < 2:
    raise ValueError('Too few coefficients. Need at least 2.')
  coefficients = list(coefficients) + list(reversed(coefficients))[1:]
  state = integrator_step_state.state
  state_grads = integrator_step_state.state_grads
  momentum = integrator_step_state.momentum
  step_size = maybe_broadcast_structure(step_size, state)

  state = tf.nest.map_structure(tf.convert_to_tensor, state)
  momentum = tf.nest.map_structure(tf.convert_to_tensor, momentum)
  state = tf.nest.map_structure(tf.convert_to_tensor, state)

  if state_grads is None:
    _, _, state_grads = call_and_grads(target_log_prob_fn, state)
  else:
    state_grads = tf.nest.map_structure(tf.convert_to_tensor, state_grads)

  for i, c in enumerate(coefficients):
    # pylint: disable=cell-var-from-loop
    if i % 2 == 0:
      momentum = tf.nest.map_structure(lambda m, sg, s: m + c * sg * s,
                                       momentum, state_grads, step_size)

      kinetic_energy, kinetic_energy_extra, momentum_grads = call_and_grads(
          kinetic_energy_fn, momentum)
    else:
      state = tf.nest.map_structure(lambda x, mg, s: x + c * mg * s, state,
                                    momentum_grads, step_size)

      target_log_prob, state_extra, state_grads = call_and_grads(
          target_log_prob_fn, state)

  return (IntegratorStepState(state, state_grads, momentum),
          IntegratorStepExtras(target_log_prob, state_extra, kinetic_energy,
                               kinetic_energy_extra))


def leapfrog_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Leapfrog integrator `TransitionOperator`.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.
  """
  coefficients = [0.5, 1.]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def ruth4_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Ruth 4th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Ruth, Ronald D. (August 1983). "A Canonical Integration Technique".
       Nuclear Science, IEEE Trans. on. NS-30 (4): 2669-2671
  """
  c = 2**(1./3)
  coefficients = (1. / (2 - c)) * np.array([0.5, 1., 0.5 - 0.5 * c, -c])
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def blanes_3_stage_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Blanes 4th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  a1 = 0.11888010966
  b1 = 0.29619504261
  coefficients = [a1, b1, 0.5 - a1, 1. - 2. * b1]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def blanes_4_stage_step(
    integrator_step_state: IntegratorStepState,
    step_size: FloatTensor,
    target_log_prob_fn: PotentialFn,
    kinetic_energy_fn: PotentialFn,
) -> Tuple[IntegratorStepState, IntegratorStepExtras]:
  """Blanes 6th order integrator `TransitionOperator`.

  See [1] for details.

  Args:
    integrator_step_state: IntegratorStepState.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    target_log_prob_fn: Target log prob fn.
    kinetic_energy_fn: Kinetic energy fn.

  Returns:
    integrator_step_state: IntegratorStepState.
    integrator_step_extras: IntegratorStepExtras.

  #### References:

  [1]: Sergio Blanes, Fernando Casas, J.M. Sanz-Serna. Numerical integrators for
       the Hybrid Monte Carlo method. SIAM J. Sci. Comput., 36(4), 2014.
       https://arxiv.org/pdf/1405.3153.pdf
  """
  a1 = 0.071353913
  a2 = 0.268548791
  b1 = 0.191667800
  coefficients = [a1, b1, a2, 0.5 - b1, 1. - 2. * (a1 + a2)]
  return symmetric_spliting_integrator_step(
      integrator_step_state,
      step_size,
      target_log_prob_fn,
      kinetic_energy_fn,
      coefficients=coefficients)


def metropolis_hastings_step(current_state: State,
                             proposed_state: State,
                             energy_change: FloatTensor,
                             log_uniform: FloatTensor = None,
                             seed=None) -> Tuple[State, tf.Tensor, tf.Tensor]:
  """Metropolis-Hastings step.

  This probabilistically chooses between `current_state` and `proposed_state`
  based on the `energy_change` so as to preserve detailed balance.

  Energy change is the negative of `log_accept_ratio`.

  Args:
    current_state: Current state.
    proposed_state: Proposed state.
    energy_change: E(proposed_state) - E(previous_state).
    log_uniform: Optional logarithm of a uniformly distributed random sample in
      [0, 1]. It is used to accept/reject the current and proposed state.
    seed: For reproducibility.

  Returns:
    new_state: The chosen state.
    is_accepted: Whether the proposed state was accepted.
    log_uniform: The random number that was used to select between the two
      states.
  """
  flat_current = tf.nest.flatten(current_state)
  flat_proposed = nest.flatten_up_to(current_state, proposed_state)
  # Impute the None's in the current state.
  flat_current = [
      p if c is None else c for p, c in zip(flat_proposed, flat_current)
  ]
  current_state = tf.nest.pack_sequence_as(current_state, flat_current)

  current_state = tf.nest.map_structure(tf.convert_to_tensor, current_state)
  proposed_state = tf.nest.map_structure(tf.convert_to_tensor, proposed_state)
  energy_change = tf.convert_to_tensor(value=energy_change)

  log_accept_ratio = -energy_change

  if log_uniform is None:
    log_uniform = tf.math.log(
        tf.random.uniform(
            shape=tf.shape(input=log_accept_ratio),
            dtype=log_accept_ratio.dtype.base_dtype,
            seed=seed))
  is_accepted = log_uniform < log_accept_ratio

  next_state = mcmc_util.choose(
      is_accepted, proposed_state, current_state, name='choose_next_state')
  return next_state, is_accepted, log_uniform


# state_extra is not a true state, but here for convenience.
HamiltonianMonteCarloState = collections.namedtuple(
    'HamiltonianMonteCarloState',
    'state, state_grads, target_log_prob, state_extra')

HamiltonianMonteCarloExtra = collections.namedtuple(
    'HamiltonianMonteCarloExtra',
    'is_accepted, log_accept_ratio, integrator_trace, '
    'proposed_hmc_state')

MomentumSampleFn = Union[Callable[[State], State], Callable[..., State]]


def hamiltonian_monte_carlo(
    hmc_state: HamiltonianMonteCarloState,
    target_log_prob_fn: PotentialFn,
    step_size: Any,
    num_integrator_steps: IntTensor,
    momentum: State = None,
    kinetic_energy_fn: PotentialFn = None,
    momentum_sample_fn: MomentumSampleFn = None,
    integrator_trace_fn: Callable[[IntegratorStepState, IntegratorStepExtras],
                                  TensorNest] = lambda *args: (),
    log_uniform: FloatTensor = None,
    integrator=leapfrog_step,
    seed=None,
) -> Tuple[HamiltonianMonteCarloState, HamiltonianMonteCarloExtra]:
  """Hamiltonian Monte Carlo `TransitionOperator`.

  #### Example

  ```python
  step_size = 0.2
  num_steps = 2000
  num_integrator_steps = 10
  state = tf.ones([16, 2])

  base_mean = [1., 0]
  base_cov = [[1, 0.5], [0.5, 1]]

  bijector = tfb.Softplus()
  base_dist = tfd.MultivariateNormalFullCovariance(
      loc=base_mean, covariance_matrix=base_cov)
  target_dist = bijector(base_dist)

  def orig_target_log_prob_fn(x):
    return target_dist.log_prob(x), ()

  target_log_prob_fn, state = fun_mcmc.transform_log_prob_fn(
      orig_target_log_prob_fn, bijector, state)

  kernel = tf.function(lambda state: fun_mcmc.hamiltonian_monte_carlo(
      state,
      step_size=step_size,
      num_integrator_steps=num_integrator_steps,
      target_log_prob_fn=target_log_prob_fn,
      seed=tfp_test_util.test_seed()))

  _, chain = fun_mcmc.trace(
      state=fun_mcmc.HamiltonianMonteCarloState(
          state=state,
          state_grads=None,
          target_log_prob=None,
          state_extra=None),
      fn=kernel,
      num_steps=num_steps,
      trace_fn=lambda state, extra: state.state_extra[0])
  ```

  Args:
    hmc_state: HamiltonianMonteCarloState.
    target_log_prob_fn: Target log prob fn.
    step_size: Step size, structure broadcastable to the `target_log_prob_fn`
      state.
    num_integrator_steps: Number of integrator steps to take.
    momentum: Initial momentum, passed to `momentum_sample_fn`. Default: zeroes.
    kinetic_energy_fn: Kinetic energy function.
    momentum_sample_fn: Sampler for the momentum.
    integrator_trace_fn: Trace function for the integrator integrator.
    log_uniform: Optional logarithm of a uniformly distributed random sample in
      [0, 1], used for the MH accept/reject step.
    integrator: Integrator to use for the HMC dynamics.
    seed: For reproducibility.

  Returns:
    hmc_state: HamiltonianMonteCarloState
    hmc_extra: HamiltonianMonteCarloExtra
  """
  state = hmc_state.state
  state_grads = hmc_state.state_grads
  target_log_prob = hmc_state.target_log_prob
  state_extra = hmc_state.state_extra

  if kinetic_energy_fn is None:

    # pylint: disable=function-redefined
    def kinetic_energy_fn(*momentum):
      return tf.add_n([
          tf.reduce_sum(input_tensor=tf.square(x), axis=-1) / 2.
          for x in tf.nest.flatten(momentum)
      ]), ()

  if momentum_sample_fn is None:

    # pylint: disable=function-redefined
    def momentum_sample_fn(*momentum):
      ret = tf.nest.map_structure(
          lambda x: tf.random.normal(tf.shape(input=x), dtype=x.dtype),
          momentum)
      if len(ret) == 1:
        return ret[0]
      else:
        return ret

  if momentum is None:
    momentum = call_fn(momentum_sample_fn,
                       tf.nest.map_structure(tf.zeros_like, state))
  if target_log_prob is None:
    target_log_prob, state_extra, state_grads = call_and_grads(
        target_log_prob_fn, state)

  kinetic_energy, _ = call_fn(kinetic_energy_fn, momentum)
  current_energy = -target_log_prob + kinetic_energy
  current_state = HamiltonianMonteCarloState(
      state=state,
      state_grads=state_grads,
      state_extra=state_extra,
      target_log_prob=target_log_prob)

  def integrator_wrapper(integrator_step_state, target_log_prob, state_extra):
    """Integrator wrapper that tracks extra state."""
    del target_log_prob
    del state_extra

    integrator_step_state, integrator_extra = integrator(
        integrator_step_state,
        step_size=step_size,
        target_log_prob_fn=target_log_prob_fn,
        kinetic_energy_fn=kinetic_energy_fn)

    return [
        integrator_step_state, integrator_extra.target_log_prob,
        integrator_extra.state_extra
    ], integrator_extra

  def integrator_trace_wrapper_fn(args, integrator_extra):
    return integrator_trace_fn(args[0], integrator_extra)

  integrator_wrapper_state = (IntegratorStepState(state, state_grads, momentum),
                              target_log_prob, state_extra)

  [[integrator_step_state, target_log_prob, state_extra],
   _], integrator_trace = trace(
       integrator_wrapper_state,
       integrator_wrapper,
       num_integrator_steps,
       trace_fn=integrator_trace_wrapper_fn)

  kinetic_energy, _ = call_fn(kinetic_energy_fn, integrator_step_state.momentum)
  proposed_energy = -target_log_prob + kinetic_energy
  proposed_state = HamiltonianMonteCarloState(
      state=integrator_step_state.state,
      state_grads=integrator_step_state.state_grads,
      target_log_prob=target_log_prob,
      state_extra=state_extra)

  energy_change = proposed_energy - current_energy
  hmc_state, is_accepted, _ = metropolis_hastings_step(
      current_state,
      proposed_state,
      energy_change,
      log_uniform=log_uniform,
      seed=seed)

  hmc_state = hmc_state  # type: HamiltonianMonteCarloState
  return hmc_state, HamiltonianMonteCarloExtra(
      is_accepted=is_accepted,
      proposed_hmc_state=proposed_state,
      log_accept_ratio=-energy_change,
      integrator_trace=integrator_trace)


def sign_adaptation(control: FloatNest,
                    output: FloatTensor,
                    set_point: FloatTensor,
                    adaptation_rate: FloatTensor = 0.01) -> FloatNest:
  """A function to do simple sign-based control of a variable.

  ```
  control = control * (1. + adaptation_rate) ** sign(output - set_point)
  ```

  Args:
    control: The control variable.
    output: The output variable.
    set_point: The set point for `output`. This function will adjust `control`
      so that `output` matches `set_point`.
    adaptation_rate: Adaptation rate.

  Returns:
    control: New control.
  """

  def _get_new_control(control, output, set_point):
    new_control = mcmc_util.choose(output > set_point,
                                   control * (1. + adaptation_rate),
                                   control / (1. + adaptation_rate))
    return new_control

  output = maybe_broadcast_structure(output, control)
  set_point = maybe_broadcast_structure(set_point, control)

  return tf.nest.map_structure(_get_new_control, control, output, set_point)
