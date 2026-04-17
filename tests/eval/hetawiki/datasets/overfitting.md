# Overfitting

Overfitting occurs when a machine learning model learns the training data too
well — including its noise and random fluctuations — and consequently performs
poorly on unseen data. It is the central challenge in supervised learning.

## Bias-Variance Trade-off

Model error on test data decomposes into three components:

    Test error = Bias² + Variance + Irreducible noise

- **High bias** (underfitting): the model is too simple to capture the true
  pattern. Both training and test error are high.
- **High variance** (overfitting): the model is too complex. Training error is
  low but test error is high.

The goal is to find a model complexity that balances these two.

## Causes

1. **Too many parameters** relative to training samples. Deep neural networks
   with millions of weights are especially prone when data is limited.
2. **Training too long**: after a point further epochs increase test error even
   as training error decreases.
3. **Noisy labels**: the model memorises label noise.
4. **Insufficient data**: the model cannot distinguish signal from noise.

## Detection

The clearest signal is the **validation curve**: plot training loss and
validation loss vs. epochs (or model complexity). Overfitting is diagnosed when:

- Training loss continues to decrease.
- Validation loss stops decreasing and starts to increase.

A large gap between training accuracy and validation accuracy (e.g. 98% vs 72%)
also indicates overfitting.

## Prevention

### Regularisation
**L2 regularisation** (weight decay) adds a penalty λ · Σwᵢ² to the loss,
shrinking weights toward zero. **L1 regularisation** adds λ · Σ|wᵢ|, which
produces sparse weights. Both are forms of **regularisation** that constrain
model complexity.

### Dropout
Randomly zero out a fraction p of neuron activations during each training step.
At test time all neurons are active but their outputs are scaled by (1 − p).
Srivastava et al. (2014) showed dropout with p = 0.5 is effective for fully
connected layers; p = 0.1–0.2 is common for convolutional layers.

### Early Stopping
Monitor validation loss during training. Stop when it has not improved for a
patience window (e.g. 10 epochs). This is a form of regularisation that avoids
over-training.

### Data Augmentation
Artificially increase training set size by applying label-preserving
transformations (crops, flips, colour jitter for images; synonym replacement for
text). Reduces effective model complexity relative to data.

### Cross-Validation
Use k-fold cross-validation to get a reliable estimate of test performance and
to tune hyperparameters without contaminating the test set.

## Relationship to Neural Networks

Neural networks with millions of parameters are the machine learning models
most vulnerable to overfitting. Techniques such as dropout, weight decay, and
early stopping were largely developed in the neural-network context. The
**double descent** phenomenon shows that extremely over-parameterised networks
can actually generalise well, complicating the classical picture.

## Key Numbers

- Dropout rate for FC layers: **p = 0.5** (Srivastava et al., 2014)
- Dropout rate for conv layers: **p = 0.1–0.2**
- L2 weight decay typical value: **1e-4** to **1e-2**
