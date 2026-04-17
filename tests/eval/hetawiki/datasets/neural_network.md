# Neural Network

A neural network (NN) is a computational model loosely inspired by biological
neurons. It consists of layers of interconnected units (neurons) that transform
an input into an output through learned weights.

## Architecture

A feedforward neural network has three kinds of layers:

- **Input layer**: receives the raw features (one neuron per feature).
- **Hidden layers**: one or more layers that learn intermediate representations.
- **Output layer**: produces the final prediction (class probabilities, a real
  value, etc.).

Each neuron computes a weighted sum of its inputs followed by a non-linear
**activation function**:

    output = f(w · x + b)

where w are weights, x is the input vector, b is a bias, and f is the activation.

## Activation Functions

| Function | Formula | Common use |
|----------|---------|------------|
| ReLU | max(0, x) | Hidden layers; default choice |
| Sigmoid | 1 / (1 + e^−x) | Binary output |
| Softmax | eˣᵢ / Σeˣⱼ | Multi-class output |
| GELU | x · Φ(x) | Transformers |
| Tanh | (eˣ − e^−x)/(eˣ + e^−x) | RNNs |

ReLU is the default activation for hidden layers because it avoids the vanishing
gradient problem and is computationally cheap.

## Universal Approximation Theorem

A neural network with at least one hidden layer and a non-polynomial activation
can approximate any continuous function on a compact subset of ℝⁿ to arbitrary
accuracy, given enough neurons. This theorem guarantees expressiveness but says
nothing about whether learning will find the right weights.

## Training

Neural networks are trained by **backpropagation** combined with **gradient
descent**. Backpropagation computes the gradient of the loss with respect to
every weight; gradient descent (or Adam) uses these gradients to update the
weights iteratively.

The training loop:
1. Forward pass → compute prediction and loss.
2. Backward pass (backpropagation) → compute gradients.
3. Weight update (gradient descent / Adam).
4. Repeat for many mini-batches and epochs.

## Overfitting

Deep neural networks have millions of parameters and can memorise training data
rather than learning to generalise. This is called **overfitting**. Symptoms:
low training loss but high validation loss.

Common remedies:
- **Dropout**: randomly zero out neurons during training (Srivastava et al., 2014).
- **Weight decay (L2 regularisation)**: penalises large weights.
- **Early stopping**: halt training when validation loss stops improving.
- **Data augmentation**: artificially expand the training set.

## Deep vs Shallow Networks

A shallow network with one hidden layer can approximate any function, but
requires exponentially many neurons. Deep networks (many layers) learn
hierarchical representations efficiently:
- Early layers detect edges/textures.
- Middle layers detect parts (ears, wheels).
- Later layers detect whole objects.

This depth efficiency is why deep learning outperforms shallow methods on
structured data (images, text, audio).

## Key Facts

- Default activation: **ReLU**
- Training algorithm: **backpropagation** + **gradient descent**
- Dropout introduced by: **Srivastava et al., 2014**
- Universal Approximation Theorem: network with **one hidden layer** can
  approximate any continuous function
