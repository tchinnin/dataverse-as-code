# Prerequisites for `dv_metadata.py`

`validate` runs offline and only needs the Python dependencies. `doctor`, `plan` and `apply` also need an Azure CLI session and the dev environment inputs.

## 1. Python + dependencies

Python ≥ 3.9 in a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r <plugin>/skills/dvac-metadata/scripts/requirements.txt
```

`pyyaml` reads the model and `jsonschema` validates it. No Dataverse SDK or auth package is needed.

## 2. Azure CLI session

The tool asks the Azure CLI for a token (`az account get-access-token --resource <DATAVERSE_URL>`). It never stores credentials. Sign in once per session:

```sh
az login --tenant <TENANT_ID>
```

When a tenant is given (`--tenant` or `TENANT_ID`), the token is requested for that tenant even if the active `az` subscription belongs to another one.

## 3. Dev environment inputs

The tool needs three inputs: the dev environment URL, its declared stage (must be `dev`), and optionally the Entra tenant. It takes them from **one** source, in this order:

1. **Command-line flags** on `doctor`, `plan`, `apply`: `--env-url https://<org>-dev.crm4.dynamics.com --stage dev [--tenant <tenant-guid>]`.
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

`publisher.solution` in `_meta.dv.metadata.yaml` must exist in the dev environment as an **unmanaged** solution whose publisher uses `publisher.prefix`. Create it with `dataverse:dv-solution`.

## 5. Check

```sh
python3 <plugin>/skills/dvac-metadata/scripts/dv_metadata.py doctor dataverse/metadata
```

Every line must show ✓ before you run `plan`.

## 6. Offline tests (tool maintainers)

```sh
python3 -m unittest discover -s <plugin>/skills/dvac-metadata/tests -v
```
