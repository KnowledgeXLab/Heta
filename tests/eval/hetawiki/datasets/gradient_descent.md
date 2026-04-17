# Gradient Descent

Gradient descent is an iterative optimisation algorithm used to minimise a loss
function by moving in the direction of the steepest descent — the negative gradient.
It is the backbone of training nearly all modern machine learning models.

## Core Idea

Given a loss function L(θ) over model parameters θ, the update rule is:

    θ ← θ − α · ∇L(θ)

where α (alpha) is the **learning rate**, a positive scalar that controls how large
each step is. If α is too large the loss oscillates or diverges; if too small,
convergence is very slow.

## Variants

| Variant | Batch size | Notes |
|---------|-----------|-------|
| Batch gradient descent | Full dataset | Stable but slow on large datasets |
| Stochastic gradient descent (SGD) | 1 sample | Noisy but fast updates |
| Mini-batch gradient descent | 32–512 samples | Best of both; standard in practice |

Mini-batch gradient descent with a batch size between 32 and 512 is the
de-facto standard for training deep learning models.

## Learning Rate Schedules

A fixed learning rate rarely achieves the best result. Common schedules:

- **Step decay**: multiply α by a factor (e.g. 0.1) every N epochs.
- **Cosine annealing**: smoothly reduce α following a cosine curve.
- **Warmup + decay**: start with a low α, ramp up for the first few thousand steps,
  then decay. Used in Transformer training.

## Momentum and Adaptive Methods

Plain gradient descent can be slow to navigate ravines in the loss surface.
Extensions add momentum or adapt the learning rate per parameter:

- **Momentum**: accumulates a velocity vector; reduces oscillation.
- **RMSProp**: divides gradient by a moving average of squared gradients.
- **Adam** (Adaptive Moment Estimation): combines momentum and RMSProp.
  Adam uses β₁ = 0.9, β₂ = 0.999, ε = 1e-8 as default hyperparameters.
  It is the most widely used optimiser in deep learning as of 2024.

## Relationship to Backpropagation

Gradient descent requires the gradient ∇L(θ). In neural networks this gradient
is computed efficiently by **backpropagation**, which applies the chain rule
layer by layer from the output back to the input. Gradient descent and
backpropagation are complementary: backpropagation computes the gradients;
gradient descent uses them to update the weights.

## Convergence and Pitfalls

- **Saddle points**: far more common than local minima in high-dimensional spaces;
  gradient descent with momentum usually escapes them.
- **Vanishing/exploding gradients**: gradients shrink or grow exponentially during
  backpropagation through deep networks; mitigated by careful initialisation,
  batch normalisation, and gradient clipping.
- **Overfitting**: after sufficient training steps the model may memorise training
  data rather than generalise; regularisation techniques address this.

## Key Numbers

- Adam default learning rate: **1e-3** (0.001)
- Typical mini-batch size: **32–512**
- Adam β₁: **0.9**, β₂: **0.999**
