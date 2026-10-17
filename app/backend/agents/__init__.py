"""Run a multi-step task as a visible, cancellable sequence of steps.

The supervisor (supervisor.py) is ordinary code that runs steps in order and
records everything it does as events. Later steps can use the AI model as a
"reader", "editor" or "reviewer", but the control flow, limits and
cancellation never depend on a model's judgement.
"""
