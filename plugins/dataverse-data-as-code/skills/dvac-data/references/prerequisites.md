# Prerequisites for `dv_data.py`

`validate` runs offline and only needs the Python dependencies. `doctor`, `plan` and `apply` also need an Azure CLI session and the inputs of the target stage.

## 1. Python + dependencies

Python ≥ 3.9 in a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r <plugin>/skills/dvac-data/scripts/requirements.txt
```

`pyyaml` reads the model and `jsonschema` validates it. No Dataverse SDK or auth package is needed.

## 2. Azure CLI session

The tool asks the Azure CLI for a token (`az account get-access-token --resource <DATAVERSE_URL>`). It never stores credentials. Sign in once per session:

```sh
az login --tenant <TENANT_ID>
```

The `az` user needs read and write access to the managed tables, and `prvBypassCustomBusinessLogic` when a table uses `bypass: [sync]` or `[async]`.

## 3. Stage inputs

Every `doctor`, `plan` and `apply` names its stage: `--stage dev`, `--stage uat`, `--stage prod`… There is no default. The environment comes from **one** source, in this order:

1. **Command-line flags**: `--env-url https://<org>.crm4.dynamics.com [--tenant <tenant-guid>]`. The stage is the one given by `--stage`.
2. **Environment variables** `DATAVERSE_URL`, `ENVIRONMENT_STAGE`, `TENANT_ID`. `ENVIRONMENT_STAGE` must equal `--stage`.
3. **A `<repo-root>/.env.<stage>` file** with the same keys. It holds no secrets and can be committed. One way to create them: the `ppbp-init` skill of the `powerplatform-bestpractices` marketplace.

```dotenv
# .env.prod
ENVIRONMENT_STAGE=prod
DATAVERSE_URL=https://<org>.crm4.dynamics.com
TENANT_ID=<tenant-guid>
```

`--tenant` overrides the tenant of any source. The tool ignores `.env`, the local file `dataverse:dv-connect` writes for its own tooling.

The tool refuses to run when:

- `--stage` is missing, or the source declares another stage
- the URL is the one of another `<repo-root>/.env.<stage>` file
- at apply, the organization id differs from the one in the plan (plan is stale)
- outside dev, at apply, the URL or the delete count typed by the user is missing or wrong

## 4. Tables and alternate keys

Each managed table must exist in the target environment with an **Active** alternate key on exactly the `key` columns. Create keys with `dvac-metadata` (`keys:`) or `dataverse:dv-metadata`, and promote them with the solution before planning data on UAT or prod.

## 5. Check

```sh
python3 <plugin>/skills/dvac-data/scripts/dv_data.py doctor dataverse/data --stage dev
```

Every line must show ✓ before you run `plan`.

## 6. Offline tests (tool maintainers)

```sh
python3 -m unittest discover -s <plugin>/skills/dvac-data/tests -v
```
