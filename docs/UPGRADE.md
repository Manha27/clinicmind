# Upgrading an existing clone to this version

Run in PowerShell from your clinicmind folder, with `(.venv)` active. Stop `uvicorn` first.

1. Extract the zip into the project folder (overwrites only the files it contains):
   `Expand-Archive -Path "$env:USERPROFILE\Downloads\clinicmind_full.zip" -DestinationPath . -Force`
2. `pip install -r requirements.txt`
3. Make sure Docker is running, then create the database migration for the new tables and apply it:
   `alembic revision --autogenerate -m "add jobs integrations tasks evals"`
   `alembic upgrade head`
   (the generated file should create 7 tables: jobs, integration_accounts, sync_cursors, oauth_states, tasks, eval_runs, eval_results)
4. `python -m pytest -q`   -> 126 passed
5. Add the new variables from `.env.example` to your `.env` (all optional except what you already have).
6. Start `uvicorn app.main:app`, and in a second terminal `python -m app.worker`. Open http://127.0.0.1:8000/ui/
7. `git add .`, check `git status` (no `.env`), commit, push.
