"""Application services: the business logic between the API and the models.

A service function takes a session and already-authorised inputs, enforces the
rules, and returns models. It never touches ``Request``, never builds a response,
and never commits -- the route owns the transaction so that a request which writes
several rows cannot half-apply.
"""
