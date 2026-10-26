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

## 3. Dev environment inputs

The tool needs three inputs: the dev environment URL, its declared stage (must be `dev`), and optionally the Entra tenant. It takes them from **one** source, in this order:

1. **Command-line flags** on `doctor`, `plan`, `apply` and `export`: `--env-url https://<org>-dev.crm4.dynamics.com --stage dev [--tenant <tenant-guid>]`.
2. **Environment variables** `DATAVERSE_URL`, `ENVIRONMENT_STAGE=dev`, `TENANT_ID` (CI, a shell profile, a secrets manager…).
3. **A `<repo-root>/.env.dev` file** with the same keys. It holds no secrets and can be committed. One way to create it: the `ppbp-init` skill of the `powerplatform-bestpractices` marketplace.

```dotenv
ENVIRONMENT_STAGE=dev
DATAVERSE_URL=https://<org>-dev.crm4.dynamics.com
TENANT_ID=<tenant-guid>
```

URL and stage always come from the same source: a `--env-url` flag never borrows the stage of a `.env.dev`. `--tenant` overrides the tenant of any source. The tool ignores `.env`, the local file `dataverse:dv-connect` writes for its own tooling.

The tool refuses to run when:

- the stage is not `dev`
- the URL matches the one in `<repo-root>/.env.uat` or `.env.prod`, when those files exist
- the target solution is missing or managed (checked on the server)

## 4. Unmanaged solution in dev

`publisher.solution` must exist in the dev environment as an **unmanaged** solution whose publisher uses `publisher.prefix`. Create it with `dataverse:dv-solution`.

## 5. PAC CLI profile and build output (apply)

- **PAC CLI.** `apply` pushes new content with `pac plugin push` and always passes `--environment <dev URL>`. The active pac profile must therefore have access to the dev environment. `dataverse:dv-connect` owns PAC CLI installation and authentication.
- **Build output.** The `.nupkg` (or `.dll`) that the model points to must exist. Build the project before planning.

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
