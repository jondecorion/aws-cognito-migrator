# Cognito-to-Cognito User Migration Tool

![CLI Demo](images/cli_demo.svg)

A lightweight, standalone Python CLI utility to migrate AWS Cognito User Pools, schemas, client app settings, user groups, and user profiles between AWS accounts or regions.

Because AWS Cognito does not support the export of hashed passwords, this tool bulk-migrates user records with secure, generated temporary passwords. Users are marked as `FORCE_CHANGE_PASSWORD` and will be prompted to set a new password on their first login.

---

## Features

* **Schema Replication:** Reads the source User Pool configuration and recreates it in the target account, automatically retaining all custom attributes (case-sensitivity and datatype constraints are preserved).
* **App Client Setup:** Copies app client profiles (name, allowed OAuth flows, callback/logout URLs, token validity periods) to the new pool.
* **Groups & Memberships:** Automatically recreates user groups (description, role ARNs, precedence) and associates migrated users to their respective groups.
* **Flexible Notification Options:**
  1. **Auto-Send Emails:** Let AWS Cognito send welcome emails containing temporary passwords directly to the users.
  2. **Suppress & Export:** Create users silently and output their temporary passwords to a local CSV file, allowing you to notify them using your own email marketing tool.

---

## Setup & Requirements

1. Make sure **Python 3** is installed:
   ```bash
   python --version
   ```

2. Install the AWS SDK dependency:
   ```bash
   pip install -r requirements.txt
   ```

3. Ensure your local terminal is authenticated with your AWS accounts (via standard credentials files or `aws sso login`).

---

## Running the Migration

The tool supports two execution modes: **Interactive Wizard** and **Automated Command Line**.

### Mode A: Interactive Wizard (Default)
Simply run the script with no arguments. The CLI will guide you step-by-step:
```bash
python migrate.py
```
1. **Source Profile:** Lists and lets you choose from your locally configured AWS profiles.
2. **Source Region:** Enter your source AWS region (defaults to `us-east-1`).
3. **Source Pool ID:** Lists all user pools in the selected region for you to choose from.
4. **Target Settings:** Select target AWS profile, region, and action (recreate or migrate into existing).
5. **Notification Preference:** Select whether to let Cognito send invite emails or output temporary passwords silently to a local CSV file.

---

### Mode B: Automated CLI (For Scripts / CI/CD)
To bypass the wizard prompts and automate execution, pass the configuration arguments directly:
```bash
python migrate.py \
  --src-profile source-profile \
  --src-pool-id us-east-1_MockPoolId123 \
  --tgt-profile target-profile \
  --tgt-pool-name my-recreated-user-pool \
  --suppress-emails \
  --csv-path ./output_credentials.csv \
  --yes
```

#### Available CLI Arguments:
* `--src-profile`: Source AWS CLI profile name (Required for automated mode)
* `--src-pool-id`: Source Cognito User Pool ID (Required for automated mode)
* `--tgt-profile`: Target AWS CLI profile name (Required for automated mode)
* `--src-region`: Region for source pool (Default: `us-east-1`)
* `--tgt-region`: Region for target pool (Default: `us-east-1`)
* `--tgt-pool-name`: Name of new target pool to create
* `--tgt-pool-id`: ID of an existing target User Pool to migrate into (bypasses pool creation)
* `--suppress-emails`: Suppress Cognito welcome notifications and generate a CSV log
* `--csv-path`: Custom path to save the output CSV credentials (Default: `./migrated_users_credentials.csv`)
* `--yes`: Bypasses the final confirmation summary block and executes immediately
* `--verbose`: Enables debug-level logging outputs in the console
