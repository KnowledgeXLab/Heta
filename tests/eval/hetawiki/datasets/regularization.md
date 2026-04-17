# Regularisation

Regularisation refers to any technique that reduces a model's tendency to
**overfit** the training data by constraining its effective complexity. It is
one of the most important practical tools in machine learning.

## Why Regularisation Is Needed

A model with more parameters than training examples can memorise the training
set perfectly (zero training loss) while failing on test data. Regularisation
introduces a cost for complexity, forcing the model to find simpler solutions
that generalise better.

## L1 Regularisation (Lasso)

Adds the sum of absolute weight values to the loss:

    L_total = L_data + λ · Σ|wᵢ|

**Effect**: drives many weights exactly to zero, producing a **sparse** model.
Useful for feature selection. The solution path is piecewise linear.

**Typical λ range**: 1e-4 to 1e-1 depending on dataset size.

## L2 Regularisation (Ridge / Weight Decay)

Adds the sum of squared weight values to the loss:

    L_total = L_data + λ · Σwᵢ²

**Effect**: shrinks all weights toward zero proportionally. Does not produce
exactly-zero weights. Equivalent to placing a Gaussian prior on weights.

In neural networks L2 regularisation is almost always implemented as
**weight decay** in the optimiser (e.g. Adam with weight_decay=1e-4) rather
than modifying the loss explicitly.

**Typical λ / weight_decay**: **1e-4** (widely used default).

## Dropout

A neural-network-specific regulariser introduced by Srivastava et al. (2014).
During training, each neuron's activation is independently set to zero with
probability p. At test time the weights are scaled by (1 − p).

Dropout can be interpreted as training an ensemble of 2^n networks with shared
weights. It is the most widely used regulariser for deep networks.

- FC layers: p = **0.5**
- Convolutional layers: p = **0.1–0.2**

## Batch Normalisation

Normalises layer activations to zero mean and unit variance per mini-batch,
then applies learnable scale and shift parameters. This stabilises training
and has a mild regularising effect (equivalent to a stochastic form of
noise injection).

Introduced by Ioffe and Szegedy (2015); standard in convolutional and
transformer architectures.

## Early Stopping

Monitors validation loss and halts training when it stops improving. Prevents
over-training without any modification to the model or loss function. Simple
and effective.

## Relationship to Overfitting

Every regularisation technique above exists to combat **overfitting**. The
right choice depends on the model:

| Technique | Best for |
|-----------|---------|
| L1 | Linear models, sparse problems |
| L2 / weight decay | Neural networks, general |
| Dropout | Fully connected layers |
| Batch norm | Deep CNNs, Transformers |
| Early stopping | Any model with iterative training |

## Key Facts

- L2 weight decay default: **1e-4**
- L1 produces: **sparse** weights (many exactly zero)
- Dropout introduced by: **Srivastava et al., 2014**
- Batch normalisation: **Ioffe and Szegedy, 2015**
