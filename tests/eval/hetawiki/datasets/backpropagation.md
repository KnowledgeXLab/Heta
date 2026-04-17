# Backpropagation

Backpropagation (backprop) is an algorithm for computing the gradient of a loss
function with respect to every weight in a neural network. It makes training
deep networks feasible by applying the **chain rule of calculus** layer by layer
in reverse, from the output back to the input.

## Historical Context

Backpropagation was popularised by Rumelhart, Hinton, and Williams in their 1986
Nature paper "Learning representations by back-propagating errors". Although the
mathematical idea predates this work, the 1986 paper demonstrated its practical
value for multi-layer networks and triggered the first wave of neural-network
research.

## Forward Pass and Loss

Training proceeds in two phases:

1. **Forward pass**: inputs propagate through each layer, producing activations
   and a final prediction ŷ. The loss L(ŷ, y) (e.g. cross-entropy) measures
   the error.

2. **Backward pass (backpropagation)**: the algorithm computes ∂L/∂w for every
   weight w by applying the chain rule:

       ∂L/∂wᵢ = ∂L/∂aⱼ · ∂aⱼ/∂zⱼ · ∂zⱼ/∂wᵢ

   where aⱼ is the post-activation output and zⱼ is the pre-activation (linear)
   output of layer j.

## Relationship to Gradient Descent

Backpropagation computes the gradients; **gradient descent** (or one of its
variants such as Adam) uses those gradients to update the weights:

    w ← w − α · ∂L/∂w

The two algorithms are inseparable in practice: backpropagation without an
update rule does nothing, and gradient descent without gradients cannot proceed.

## Computational Graph Perspective

Modern deep-learning frameworks (PyTorch, JAX, TensorFlow) implement backprop
through **automatic differentiation** on a computational graph. Each operation
records how to compute its local gradient; the backward pass chains them.

This generalises beyond standard neural networks to any differentiable program.

## Vanishing and Exploding Gradients

In deep networks the chain rule multiplies many local Jacobians together.
If they are all < 1 the product shrinks exponentially — **vanishing gradients**.
If they are all > 1 the product explodes — **exploding gradients**.

Solutions include:
- **ReLU activations** (gradient = 1 for positive inputs) instead of sigmoid.
- **Residual connections** (skip connections in ResNets).
- **Gradient clipping**: cap the norm of the gradient vector before the update.
- **Batch normalisation**: normalise activations between layers.

## Computational Complexity

For a network with L layers each of width n:
- Forward pass: O(L · n²) multiplications.
- Backward pass: approximately **2× the cost of the forward pass**.

## Key Facts

- Popularised by: **Rumelhart, Hinton, Williams (1986)**
- Publication: *Nature*, vol. 323, pp. 533–536
- Backward pass cost: ~**2×** the forward pass
- Core mathematical tool: **chain rule of calculus**
