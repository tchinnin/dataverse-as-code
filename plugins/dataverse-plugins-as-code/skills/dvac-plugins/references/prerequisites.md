# Prerequisites for `dv_plugin.py`

| Command | Needs |
|---|---|
| `validate` | Runs offline; needs only the Python dependencies (step 1). |
| `doctor`, `plan`, `export` | Steps 1 to 4. |
| `apply` | Steps 1 to 5, including the build output and the PAC CLI. |

## 1. Python + dependencies

Python ≥ 3.9 in a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r <plugin>/skills/dvac-plugins/scripts/requirements.txt
```

`pyyaml` reads the model and `jsonschema` validates it. No Dataverse SDK or auth package is needed.

## 2. Azure CLI session (Web API)

The tool asks the Azure CLI for a token (`az account get-access-token --resource <DATAVERSE_URL>`) and never stores credentials. Sign in once per session:

```sh
az login --tenant <TENANT_ID>
```

## 3. `.env.dev` at the repo root

`ppbp-init` creates this file. It is committed and holds no secrets:

```dotenv
ENVIRONMENT_STAGE=dev
DATAVERSE_URL=https://<org>-dev.crm4.dynamics.com
TENANT_ID=<tenant-guid>
```

The tool reads **only** this file. It refuses to run in either of these cases:

- `ENVIRONMENT_STAGE` is not `dev`
- the URL matches the one in `.env.uat` or `.env.prod`

## 4. Unmanaged solution in dev

`publisher.solution` must exist in the dev environment as an **unmanaged** solution whose publisher uses `publisher.prefix`. Create it with `dataverse:dv-solution`.

## 5. PAC CLI profile and build output (apply)

- **PAC CLI.** `apply` pushes new content with `pac plugin push` and always passes `--environment <DATAVERSE_URL>`. The active pac profile must therefore have access to the dev environment. `dataverse:dv-connect` owns PAC CLI installation and authentication.
- **Build output.** The `.nupkg` (or `.dll`) that the model points to must exist. Build it as described in `ppbp-dv-plugin-build`.

## 6. Check

```sh
python3 <plugin>/skills/dvac-plugins/scripts/dv_plugin.py doctor plugins/
```

Every line must show ✓ before you run `plan`.

## 7. Offline tests (tool maintainers)

```sh
python3 -m unittest discover -s <plugin>/skills/dvac-plugins/tests -v
```

One test compares the copied dev guard and Web API client with `dv_metadata.py` (`dataverse-metadata-as-code`). The plugins install independently, so the code is copied rather than imported. When you change one copy, change the other.
