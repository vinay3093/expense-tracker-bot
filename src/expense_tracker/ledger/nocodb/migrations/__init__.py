"""Alembic migrations for the Postgres + NocoDB edition.

Run with::

    alembic -c alembic.ini upgrade head      # apply
    alembic -c alembic.ini downgrade -1      # roll back one
    alembic -c alembic.ini history            # show all
    alembic -c alembic.ini current            # show current rev

The ``expense --init-postgres`` CLI command also creates the schema,
but it does so by calling ``Base.metadata.create_all()`` directly —
fine for first-time setup, but you should switch to Alembic for any
schema change after deployment so the change is auditable.
"""
