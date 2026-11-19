"""``python -m archive_mirror`` runs the CLI.

Guarded, and the import is inside the guard: worker processes are started with ``spawn``,
which imports this module again in every worker (as ``__mp_main__``).
"""

if __name__ == "__main__":
    from archive_mirror.cli import app

    app()
