"""Code review: rule tables, the review itself, and what it found.

The pieces are deliberately separable. ``rules`` and ``xlsx`` know nothing about
a model, ``parse`` knows nothing about the network, and ``reviewer`` is the only
one that makes a call -- so the riskiest part of the feature, reading a small
model's reply, is testable without one.
"""
