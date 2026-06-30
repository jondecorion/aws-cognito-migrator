import argparse
import csv
import logging
import os
import secrets
import string
import sys
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger("cognito_migrator")


def setup_logging(verbose: bool = False) -> None:
    log_level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(handler)


def generate_temp_password(length: int = 12) -> str:
    # Generates a strong random password meeting Cognito's password complexity rules
    alphabet = string.ascii_letters + string.digits + "!@#$%^*()-_=+"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in password)
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
                and any(c in "!@#$%^*()-_=+" for c in password)):
            return password


def get_interactive_choice(options: List[str], prompt: str) -> str:
    print(f"\n{prompt}")
    for idx, opt in enumerate(options, 1):
        print(f"  [{idx}] {opt}")
    while True:
        val = input("Enter choice number: ").strip()
        try:
            choice_idx = int(val) - 1
            if 0 <= choice_idx < len(options):
                return options[choice_idx]
        except ValueError:
            pass
        print(f"Invalid option. Please choose a number from 1 to {len(options)}")


def get_input(prompt: str, default: Optional[str] = None) -> str:
    display_prompt = f"{prompt} [{default}]: " if default else f"{prompt}: "
    val = input(display_prompt).strip()
    return val if val else (default or "")


def choose_aws_profile(prompt: str) -> str:
    try:
        session = boto3.Session()
        profiles = session.available_profiles
    except Exception:
        profiles = []
        
    if not profiles:
        return get_input(prompt, default="default")
        
    options = profiles.copy()
    options.append("Enter profile manually")
    
    selected = get_interactive_choice(options, f"Select {prompt}:")
    if selected == "Enter profile manually":
        return get_input(prompt)
    return selected


def choose_cognito_pool(session: boto3.Session, region: str, prompt: str) -> str:
    try:
        client = session.client("cognito-idp", region_name=region)
        pools_resp = client.list_user_pools(MaxResults=60)
        pools = pools_resp.get("UserPools", [])
    except Exception as e:
        logger.warning("Could not list user pools automatically: %s", e)
        pools = []
        
    if not pools:
        return get_input(prompt)
        
    options = [f"{p['Name']} ({p['Id']})" for p in pools]
    options.append("Enter User Pool ID manually")
    
    selected = get_interactive_choice(options, f"Select {prompt}:")
    if selected == "Enter User Pool ID manually":
        return get_input(prompt)
        
    selected_idx = options.index(selected)
    return pools[selected_idx]["Id"]


def clean_constraints(constraints: Dict[str, Any]) -> Dict[str, str]:
    if not constraints:
        return {}
    return {k: str(v) for k, v in constraints.items()}


def clean_admin_create_user_config(config: Dict[str, Any]) -> Dict[str, Any]:
    if not config:
        return {}
    cleaned = config.copy()
    # UnusedAccountValidityDays is deprecated in Cognito and triggers validation failures
    if "UnusedAccountValidityDays" in cleaned:
        del cleaned["UnusedAccountValidityDays"]
    return cleaned


def replicate_user_pool(
    tgt_cognito: Any, 
    src_pool: Dict[str, Any], 
    tgt_pool_name: str
) -> str:
    logger.info("Rebuilding Target User Pool Schema...")
    schema = []
    
    # Required attributes (like email) must be specified at pool creation
    schema.append({
        "Name": "email",
        "AttributeDataType": "String",
        "Mutable": True,
        "Required": True
    })
    
    # Map custom schemas from the source user pool
    for attr in src_pool.get("SchemaAttributes", []):
        name = attr["Name"]
        if name.startswith("custom:"):
            stripped_name = name[len("custom:"):]
            cleaned_attr = {
                "Name": stripped_name,
                "AttributeDataType": attr["AttributeDataType"],
                "Mutable": attr.get("Mutable", True),
                "Required": attr.get("Required", False)
            }
            if "StringAttributeConstraints" in attr:
                cleaned_attr["StringAttributeConstraints"] = clean_constraints(attr["StringAttributeConstraints"])
            if "NumberAttributeConstraints" in attr:
                cleaned_attr["NumberAttributeConstraints"] = clean_constraints(attr["NumberAttributeConstraints"])
            schema.append(cleaned_attr)
            
    create_params = {
        "PoolName": tgt_pool_name,
        "Policies": src_pool.get("Policies", {}),
        "Schema": schema,
        "UsernameAttributes": src_pool.get("UsernameAttributes", ["email"]),
        "AutoVerifiedAttributes": ["email"],
        "EmailVerificationMessage": src_pool.get("EmailVerificationMessage"),
        "EmailVerificationSubject": src_pool.get("EmailVerificationSubject"),
        "VerificationMessageTemplate": src_pool.get("VerificationMessageTemplate", {}),
        "MfaConfiguration": src_pool.get("MfaConfiguration", "OFF"),
        "EmailConfiguration": {
            "EmailSendingAccount": "COGNITO_DEFAULT"
        },
        "UserPoolTags": src_pool.get("UserPoolTags", {}),
        "AdminCreateUserConfig": clean_admin_create_user_config(src_pool.get("AdminCreateUserConfig", {})),
        "UsernameConfiguration": src_pool.get("UsernameConfiguration", {}),
        "AccountRecoverySetting": src_pool.get("AccountRecoverySetting", {})
    }
    
    try:
        new_pool_resp = tgt_cognito.create_user_pool(**create_params)
        tgt_pool_id = new_pool_resp["UserPool"]["Id"]
        logger.info("Successfully created Target User Pool: %s", tgt_pool_id)
        return tgt_pool_id
    except ClientError as e:
        logger.error("Failed to create User Pool: %s", e.response["Error"]["Message"])
        sys.exit(1)


def replicate_app_clients(
    src_cognito: Any,
    tgt_cognito: Any,
    src_pool_id: str,
    tgt_pool_id: str
) -> None:
    logger.info("Replicating User Pool App Clients...")
    try:
        clients = src_cognito.list_user_pool_clients(UserPoolId=src_pool_id, MaxResults=10).get("UserPoolClients", [])
        for c_summary in clients:
            c_details = src_cognito.describe_user_pool_client(
                UserPoolId=src_pool_id, ClientId=c_summary["ClientId"]
            )["UserPoolClient"]
            
            client_params = {
                "UserPoolId": tgt_pool_id,
                "ClientName": c_details["ClientName"],
                "RefreshTokenValidity": c_details.get("RefreshTokenValidity"),
                "AccessTokenValidity": c_details.get("AccessTokenValidity"),
                "IdTokenValidity": c_details.get("IdTokenValidity"),
                "TokenValidityUnits": c_details.get("TokenValidityUnits", {}),
                "ReadAttributes": c_details.get("ReadAttributes", []),
                "WriteAttributes": c_details.get("WriteAttributes", []),
                "ExplicitAuthFlows": c_details.get("ExplicitAuthFlows", []),
                "SupportedIdentityProviders": c_details.get("SupportedIdentityProviders", ["COGNITO"]),
                "CallbackURLs": c_details.get("CallbackURLs", []),
                "LogoutURLs": c_details.get("LogoutURLs", []),
                "AllowedOAuthFlows": c_details.get("AllowedOAuthFlows", []),
                "AllowedOAuthScopes": c_details.get("AllowedOAuthScopes", []),
                "AllowedOAuthFlowsUserPoolClient": c_details.get("AllowedOAuthFlowsUserPoolClient", True),
                "PreventUserExistenceErrors": c_details.get("PreventUserExistenceErrors", "ENABLED"),
                "EnableTokenRevocation": c_details.get("EnableTokenRevocation", True),
                "AuthSessionValidity": c_details.get("AuthSessionValidity", 3)
            }
            
            new_client = tgt_cognito.create_user_pool_client(**client_params)
            logger.info("Created App Client '%s': %s", c_details["ClientName"], new_client["UserPoolClient"]["ClientId"])
    except ClientError as e:
        logger.warning("Failed to copy app client configuration: %s", e.response["Error"]["Message"])


def replicate_user_groups(
    src_cognito: Any,
    tgt_cognito: Any,
    src_pool_id: str,
    tgt_pool_id: str
) -> Dict[str, List[str]]:
    logger.info("Mapping user groups and memberships...")
    groups_mapping: Dict[str, List[str]] = {}
    try:
        groups = []
        paginator = src_cognito.get_paginator("list_groups")
        for page in paginator.paginate(UserPoolId=src_pool_id):
            groups.extend(page.get("Groups", []))
            
        for group in groups:
            group_name = group["GroupName"]
            
            try:
                tgt_cognito.create_group(
                    UserPoolId=tgt_pool_id,
                    GroupName=group_name,
                    Description=group.get("Description", ""),
                    RoleArn=group.get("RoleArn", ""),
                    Precedence=group.get("Precedence", 0)
                )
                logger.info("Recreated group '%s' in target pool.", group_name)
            except ClientError as e:
                if e.response["Error"]["Code"] != "GroupExistsException":
                    logger.warning("Failed to create group %s: %s", group_name, e.response["Error"]["Message"])
            
            members = []
            m_paginator = src_cognito.get_paginator("list_users_in_group")
            for m_page in m_paginator.paginate(UserPoolId=src_pool_id, GroupName=group_name):
                members.extend(m_page.get("Users", []))
                
            for m in members:
                username = m["Username"]
                if username not in groups_mapping:
                    groups_mapping[username] = []
                groups_mapping[username].append(group_name)
    except ClientError as e:
        logger.warning("Failed to fetch group configurations: %s", e.response["Error"]["Message"])
        
    return groups_mapping


def migrate_users(
    src_cognito: Any,
    tgt_cognito: Any,
    src_pool_id: str,
    tgt_pool_id: str,
    groups_mapping: Dict[str, List[str]],
    is_email_username: bool,
    suppress_emails: bool,
    csv_file_path: Optional[str]
) -> None:
    logger.info("Fetching source Cognito users...")
    users = []
    try:
        u_paginator = src_cognito.get_paginator("list_users")
        for page in u_paginator.paginate(UserPoolId=src_pool_id):
            users.extend(page.get("Users", []))
        logger.info("Found %d users in source pool.", len(users))
    except ClientError as e:
        logger.error("Failed to fetch source users: %s", e.response["Error"]["Message"])
        sys.exit(1)
        
    logger.info("Migrating %d users to target pool...", len(users))
    csv_rows = []
    
    for idx, user in enumerate(users, 1):
        username = user["Username"]
        email = None
        user_attrs = []
        
        for attr in user.get("Attributes", []):
            if attr["Name"] == "sub":
                continue
            if attr["Name"] == "email":
                email = attr["Value"]
            user_attrs.append({
                "Name": attr["Name"],
                "Value": attr["Value"]
            })
            
        # Cognito expects email as the Username parameter if UsernameAttributes includes 'email'
        creation_username = email if (is_email_username and email) else username
        temp_password = generate_temp_password()
        
        creation_args = {
            "UserPoolId": tgt_pool_id,
            "Username": creation_username,
            "UserAttributes": user_attrs,
            "TemporaryPassword": temp_password,
        }
        
        if suppress_emails:
            creation_args["MessageAction"] = "SUPPRESS"
            
        try:
            tgt_cognito.admin_create_user(**creation_args)
            
            # Map user to their corresponding target groups
            user_groups = groups_mapping.get(username, [])
            for grp in user_groups:
                try:
                    tgt_cognito.admin_add_user_to_group(
                        UserPoolId=tgt_pool_id,
                        Username=creation_username,
                        GroupName=grp
                    )
                except ClientError as e:
                    logger.warning("Failed to add %s to group %s: %s", creation_username, grp, e.response["Error"]["Message"])
            
            if suppress_emails:
                csv_rows.append({
                    "Username": creation_username,
                    "Email": email if email else "N/A",
                    "TemporaryPassword": temp_password
                })
                
            logger.info(" [%d/%d] Successfully migrated user: %s", idx, len(users), creation_username)
        except ClientError as e:
            if e.response["Error"]["Code"] == "UsernameExistsException":
                logger.info(" [%d/%d] User %s already exists in target pool. Skipping.", idx, len(users), creation_username)
            else:
                logger.error(" [%d/%d] Failed to migrate user %s: %s", idx, len(users), creation_username, e.response["Error"]["Message"])
                
    if suppress_emails and csv_rows and csv_file_path:
        try:
            with open(csv_file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["Username", "Email", "TemporaryPassword"])
                writer.writeheader()
                writer.writerows(csv_rows)
            logger.info("Saved temporary passwords for %d users in: %s", len(csv_rows), os.path.abspath(csv_file_path))
        except Exception as e:
            logger.error("Failed to write CSV file: %s", e)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cognito-to-Cognito User Migration Utility")
    parser.add_argument("--src-profile", type=str, help="AWS CLI profile for the source account")
    parser.add_argument("--src-region", type=str, default="us-east-1", help="AWS region for the source pool")
    parser.add_argument("--src-pool-id", type=str, help="Source Cognito User Pool ID")
    parser.add_argument("--tgt-profile", type=str, help="AWS CLI profile for the target account")
    parser.add_argument("--tgt-region", type=str, default="us-east-1", help="AWS region for the target pool")
    parser.add_argument("--tgt-pool-name", type=str, help="Name for the newly created target User Pool")
    parser.add_argument("--tgt-pool-id", type=str, help="Use an existing target pool ID (bypasses pool creation)")
    parser.add_argument("--suppress-emails", action="store_true", help="Suppress Cognito invite emails and write to CSV")
    parser.add_argument("--csv-path", type=str, default="./migrated_users_credentials.csv", help="Output path for credentials CSV")
    parser.add_argument("--yes", action="store_true", help="Skip the final confirmation prompt")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logging")
    return parser.parse_args()


def run_interactive_wizard() -> Tuple[str, str, str, str, str, bool, Optional[str], Optional[str], Optional[str]]:
    print("=========================================================")
    print("      * Cognito-to-Cognito User Migration Tool *")
    print("=========================================================")
    
    print("\n--- [1] Source AWS Configuration ---")
    src_profile = choose_aws_profile("Source AWS Profile")
    src_region = get_input("Source AWS Region", default="us-east-1")
    
    try:
        src_session = boto3.Session(profile_name=src_profile, region_name=src_region)
        src_cognito = src_session.client("cognito-idp")
    except Exception as e:
        logger.error("Failed to initialize Source AWS Session: %s", e)
        sys.exit(1)
        
    src_pool_id = choose_cognito_pool(src_session, src_region, "Source Cognito User Pool ID")
    if not src_pool_id:
        logger.error("Source User Pool ID is required.")
        sys.exit(1)
        
    print("\n--- [2] Target AWS Configuration ---")
    tgt_profile = choose_aws_profile("Target AWS Profile")
    tgt_region = get_input("Target AWS Region", default=src_region)
    
    try:
        tgt_session = boto3.Session(profile_name=tgt_profile, region_name=tgt_region)
        tgt_cognito = tgt_session.client("cognito-idp")
    except Exception as e:
        logger.error("Failed to initialize Target AWS Session: %s", e)
        sys.exit(1)
        
    try:
        src_pool = src_cognito.describe_user_pool(UserPoolId=src_pool_id)["UserPool"]
    except ClientError as e:
        logger.error("Failed to read source pool: %s", e.response["Error"]["Message"])
        sys.exit(1)
        
    pool_name_choice = get_interactive_choice(
        [
            "Recreate pool with same name as source", 
            "Recreate pool with a new custom name",
            "Migrate users into an existing target pool"
        ],
        "Configure Target User Pool Action:"
    )
    
    create_new_pool = True
    tgt_pool_id = None
    tgt_pool_name = ""
    
    if pool_name_choice == "Recreate pool with same name as source":
        tgt_pool_name = src_pool["Name"]
    elif pool_name_choice == "Recreate pool with a new custom name":
        tgt_pool_name = get_input("Enter target User Pool Name")
        if not tgt_pool_name:
            logger.error("User Pool Name is required.")
            sys.exit(1)
    else:
        create_new_pool = False
        tgt_pool_id = choose_cognito_pool(tgt_session, tgt_region, "Target Cognito User Pool ID")
        if not tgt_pool_id:
            logger.error("Target User Pool ID is required.")
            sys.exit(1)
        
        try:
            tgt_pool = tgt_cognito.describe_user_pool(UserPoolId=tgt_pool_id)["UserPool"]
            tgt_pool_name = tgt_pool["Name"]
        except ClientError as e:
            logger.error("Failed to read target pool: %s", e.response["Error"]["Message"])
            sys.exit(1)
            
    if create_new_pool:
        try:
            target_pools_resp = tgt_cognito.list_user_pools(MaxResults=60)
            existing_pools = target_pools_resp.get("UserPools", [])
            matching_pools = [p for p in existing_pools if p["Name"] == tgt_pool_name]
            
            if matching_pools:
                print(f"\n⚠️  WARNING: A User Pool named '{tgt_pool_name}' already exists in the target account.")
                print("Found existing pools:")
                for p in matching_pools:
                    print(f"  - ID: {p['Id']}")
                
                use_existing = get_input("Would you like to migrate into the existing pool instead of creating a duplicate? (y/n)", default="y")
                if use_existing.lower() == "y":
                    create_new_pool = False
                    if len(matching_pools) == 1:
                        tgt_pool_id = matching_pools[0]["Id"]
                    else:
                        options = [p["Id"] for p in matching_pools]
                        tgt_pool_id = get_interactive_choice(options, "Select which existing User Pool ID to use:")
                    print(f"Switching target to existing User Pool: {tgt_pool_id}")
        except Exception as e:
            logger.warning("Could not check existing target pools: %s", e)
            
    email_choice = get_interactive_choice(
        [
            "Let Cognito send welcome emails (users get automatic notification)",
            "Suppress Cognito emails and save temporary passwords to a local CSV file"
        ],
        "Choose Email Notification Strategy:"
    )
    suppress_emails = email_choice.startswith("Suppress")
    
    csv_file_path = None
    if suppress_emails:
        csv_file_path = get_input("Enter path to save the output credentials CSV file", default="./migrated_users_credentials.csv")
        
    return src_profile, src_region, src_pool_id, tgt_profile, tgt_region, create_new_pool, tgt_pool_name, tgt_pool_id, suppress_emails, csv_file_path


def main() -> None:
    args = parse_args()
    setup_logging(args.verbose)
    
    is_automated = bool(args.src_profile and args.src_pool_id and args.tgt_profile)
    
    if is_automated:
        src_profile = args.src_profile
        src_region = args.src_region
        src_pool_id = args.src_pool_id
        tgt_profile = args.tgt_profile
        tgt_region = args.tgt_region
        suppress_emails = args.suppress_emails
        csv_file_path = args.csv_path
        
        if args.tgt_pool_id:
            create_new_pool = False
            tgt_pool_id = args.tgt_pool_id
            tgt_pool_name = "Existing Pool"
        else:
            create_new_pool = True
            tgt_pool_name = args.tgt_pool_name or "Replicated-UserPool"
            tgt_pool_id = None
    else:
        (src_profile, src_region, src_pool_id, tgt_profile, tgt_region, 
         create_new_pool, tgt_pool_name, tgt_pool_id, suppress_emails, csv_file_path) = run_interactive_wizard()
         
    try:
        src_session = boto3.Session(profile_name=src_profile, region_name=src_region)
        src_cognito = src_session.client("cognito-idp")
        tgt_session = boto3.Session(profile_name=tgt_profile, region_name=tgt_region)
        tgt_cognito = tgt_session.client("cognito-idp")
    except Exception as e:
        logger.error("Failed to initialize AWS sessions: %s", e)
        sys.exit(1)
        
    try:
        src_pool = src_cognito.describe_user_pool(UserPoolId=src_pool_id)["UserPool"]
    except ClientError as e:
        logger.error("Failed to describe source User Pool: %s", e.response["Error"]["Message"])
        sys.exit(1)
        
    print("\n=========================================================")
    print("                   MIGRATION SUMMARY")
    print("=========================================================")
    print(f" Source Account Profile:  {src_profile} (Region: {src_region})")
    print(f" Source User Pool ID:     {src_pool_id} ({src_pool['Name']})")
    print(f" Target Account Profile:  {tgt_profile} (Region: {tgt_region})")
    print(f" Target Action:           {'CREATE NEW POOL' if create_new_pool else 'USE EXISTING POOL'}")
    print(f" Target Pool Name/ID:     {tgt_pool_name if create_new_pool else tgt_pool_id}")
    print(f" Email Strategy:          {'SUPPRESS (Output to CSV)' if suppress_emails else 'AUTO-SEND BY COGNITO'}")
    if csv_file_path and suppress_emails:
        print(f" CSV Target Path:         {csv_file_path}")
    print("=========================================================")
    
    if not args.yes:
        confirm = get_input("Do you want to proceed with the migration? (y/n)", default="n")
        if confirm.lower() != "y":
            logger.info("Migration cancelled.")
            sys.exit(0)
            
    if create_new_pool:
        tgt_pool_id = replicate_user_pool(tgt_cognito, src_pool, tgt_pool_name)
        replicate_app_clients(src_cognito, tgt_cognito, src_pool_id, tgt_pool_id)
    else:
        logger.info("Using existing target User Pool: %s", tgt_pool_id)
        
    groups_mapping = replicate_user_groups(src_cognito, tgt_cognito, src_pool_id, tgt_pool_id)
    
    is_email_username = "email" in src_pool.get("UsernameAttributes", [])
    migrate_users(
        src_cognito=src_cognito,
        tgt_cognito=tgt_cognito,
        src_pool_id=src_pool_id,
        tgt_pool_id=tgt_pool_id,
        groups_mapping=groups_mapping,
        is_email_username=is_email_username,
        suppress_emails=suppress_emails,
        csv_file_path=csv_file_path
    )
    
    print("\n=========================================================")
    print("Cognito-to-Cognito Migration Completed Successfully!")
    print("=========================================================")
    print(f"Target Pool ID: {tgt_pool_id}")
    print("=========================================================")


if __name__ == "__main__":
    main()
