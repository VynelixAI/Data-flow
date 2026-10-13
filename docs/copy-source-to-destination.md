# Copying a source to a destination

This is the path the chat agent takes when you name a Unity Catalog source, a destination, and a primary key such as `order_id`. No rows are written until you review the notebook and choose **Run the copy**.

A source or destination is either a table (`catalog.schema.table`) or a CSV/Excel file (`/Volumes/catalog/schema/volume/file.csv`). In `/Volumes/workspace/default/customervolume/file.csv`, the catalog is `workspace`, the schema is `default`, and the volume is `customervolume`.

The notebook that runs on Databricks is not a file in this repository. The API builds it, shows it in the code panel, then uploads it to `/Workspace/Shared/dataflow-agent/generated/copy_and_validate`.

## 1. You send the request

The chat box posts the message to `POST /api/chat`.

| What happens | File |
|---|---|
| Chat box, review card, and code panel | `frontend/src/components/Chat.tsx`, `frontend/src/App.tsx` |
| HTTP entry point | `backend/app/main.py` (`chat`) |
| Turns the request into a stream of events | `backend/app/agent/agent.py` (`stream_agent`) |

## 2. The agent decides which tools to call

LangGraph runs one model step, then the tools that step requested. The model is told to use the paths you wrote, to ignore notebook code such as `dbutils.widgets.text`, and not to copy until you approve the notebook.

| What happens | File |
|---|---|
| Graph: model, tools, then a pause for your decision | `backend/app/agent/graph.py` |
| Instructions for the model | `backend/app/agent/prompts.py` |
| Forces the tool to use the two paths from your message | `backend/app/agent/tools.py` (`bind_user_locations`) |
| Accepts `/Volumes/...` and `catalog.schema.table`; rejects `dbutils` as a catalog | `backend/app/databricks/identifiers.py` |

## 3. The agent checks that both objects exist

`generate_pipeline` looks up the source and the destination in Unity Catalog.

- If the source is missing, it stops.
- If the destination is missing, it asks you whether to create that exact path. Saying yes creates an empty file or table with the source columns. It does not copy rows yet.
- If the destination already exists, it continues to the plan.

| What happens | File |
|---|---|
| Tool `generate_pipeline` | `backend/app/agent/tools.py` (`_generate`) |
| Existence check | `backend/app/databricks/client.py` (`describe_uc_object`) |
| Create the empty destination after you say yes | `backend/app/databricks/destination.py` (`create_missing_destination`) |
| Pause until you answer | `backend/app/agent/graph.py` (`approve_node`) |

## 4. It builds the validation plan and the quality checks

The plan compares every column that exists on both sides, using the primary key you named. It also builds DQX checks from the source column names (dates, amounts, emails, JSON, and so on). Those checks are pass conditions: a row fails only when the expression is false.

| What happens | File |
|---|---|
| Column list for a table or volume file | `backend/app/agent/tools.py` (`_columns_for_copy`) |
| Source-to-target SQL (counts, nulls, duplicates, value mismatches) | `backend/app/databricks/validation.py` (`build_validation_plan`) |
| DQX rule list | `backend/app/databricks/dqx_utils.py` (`build_dqx_checks`, `infer_quality_checks`) |
| Long-term lessons from earlier runs | `backend/app/agent/memory.py` (`recall`) |
| Skill that says how a DQX check is shaped | `skills/dataflow-quality/SKILL.md` |

## 5. The review checks the plan before any write

`review_copy` blocks the copy when the source and destination are the same object, a primary key is missing, or the two sides share no columns. When the plan is acceptable, it writes short notes: the shared columns, the primary key, the write mode, and the quality-check names.

Those notes live only in memory for this chat thread. They are discarded when the copy finishes, when you choose **Don't run**, or when the chat session ends.

| What happens | File |
|---|---|
| Plan check and the notes you see | `backend/app/agent/review.py` (`review_copy`) |
| Short-term memory for this run | `backend/app/agent/session_memory.py` |
| Session cleanup | `backend/app/agent/agent.py` (`close_thread`) |

## 6. The notebook is generated and shown to you

`render_pipeline_notebook` writes one Python notebook and the code panel displays it. The copy has not started. You can add a comment, ask for a revision, or run it.

The notebook cells, in order, are:

1. **Widgets and helpers.** Source, destination, primary keys, and write mode are widget values. Helpers read a table or a `/Volumes` CSV/Excel file.
2. **Read.** Load the source. If the timestamp column is absent, it reads the whole object. Then it profiles that source frame: row count, nulls, and duplicate keys.
3. **Repair.** Trim, cast, or parse columns only when the plan asked for a repair.
4. **Projection.** Keep the mapped columns, or the full frame when there is no mapping.
5. **Quality.** Build DQX checks from the source columns and apply them. Rows with errors go to quarantine. Warning-only rows stay in the valid output.
6. **Write.** Valid rows go to the destination. Quarantine rows go to a sibling file or table whose name ends in `_quarantine`. A volume file is written as CSV or Excel. A table is written as Delta (`merge`, `overwrite`, or `append`).
7. **Validate.** Run the source-to-target SQL from step 4 and exit with JSON: valid rows, quarantine rows, DQX hits, and the source profile.

| What happens | File |
|---|---|
| Notebook text | `backend/app/databricks/notebook_executor.py` (`render_pipeline_notebook`) |
| Code panel | `frontend/src/App.tsx`, `frontend/src/components/CodeViewer.tsx` |
| Review card: Run the copy, Revise with this comment, Don't run | `frontend/src/components/Chat.tsx` |

A comment on **Run the copy** is added at the top of the notebook as an operator note. **Revise with this comment** builds a new notebook and waits again. **Don't run** writes nothing.

## 7. You approve, and Databricks runs the notebook

`run_prepared_copy` uploads the notebook and submits a serverless job. The job parameters are the widget values from the plan, including `confirm_create_destination=true`.

| What happens | File |
|---|---|
| Resume after you click Run | `backend/app/agent/graph.py` (`_finish_reviewed_run`) |
| Execute the stored notebook and then drop the short-term notes | `backend/app/agent/tools.py` (`run_prepared_copy`, `_run_copy`) |
| Upload and job submit | `backend/app/databricks/notebook_executor.py` (`upload_notebook`, `execute_notebook`) |
| Workspace folder | `backend/app/databricks/client.py` (`notebook_dir`) |

The uploaded notebook path is `/Workspace/Shared/dataflow-agent/generated/copy_and_validate`.

## 8. The result comes back to the chat

The notebook's exit JSON is turned into a validation report and a quarantine preview. The chat reports pass or fail, row counts, and each DQX rule that failed, with its count and sample keys. A finished copy can also store a long-term lesson for a later source with the same columns. That lesson file is separate from the short-term notes, which are already gone.

| What happens | File |
|---|---|
| Report assembly | `backend/app/databricks/validation.py` (`assemble_report`, `merge_dqx_quarantine`) |
| Long-term lesson after a finished copy | `backend/app/agent/memory.py` (`observe_episode`) |
| Validation and quarantine panels | `frontend/src/components/ValidationReport.tsx` |

## What does not copy data

`POST /api/generate` only returns notebook text. `POST /api/validate` only reads both sides and compares them. Neither one is the chat copy. The chat copy is `generate_pipeline` followed by your approval, which calls `run_prepared_copy`.
