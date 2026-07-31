"""
Manage deployed Agent Engine instances.

This script provides utilities to list, query, and manage deployed agents.
"""

import argparse
import asyncio
import os
import sys


def list_agents(project_id: str, location: str):
    """List all deployed Agent Engine instances"""
    from vertexai import Client

    client = Client(project=project_id, location=location)

    print(f"📋 Listing Agent Engine instances in {project_id} ({location})...")
    print()

    try:
        engines = list(client.agent_engines.list())

        if not engines:
            print("No Agent Engine instances found.")
            return

        for i, engine in enumerate(engines, 1):
            resource = engine.api_resource
            print(f"{i}. {resource.display_name}")
            print(f"   Resource Name: {resource.name}")
            print(f"   Description: {resource.description or 'N/A'}")
            print(f"   Create Time: {resource.create_time}")
            print()

    except Exception as e:
        print(f"❌ Error listing agents: {str(e)}")
        sys.exit(1)


async def _query_agent_async(client, resource_name: str, message: str, user_id: str):
    app = client.agent_engines.get(name=resource_name)

    session = await app.async_create_session(user_id=user_id)
    print(f"📝 Session ID: {session['id']}")

    print("🔄 Processing...")
    print()

    print("=" * 70)
    print("📨 Response:")
    print()

    async for event in app.async_stream_query(user_id=user_id, session_id=session["id"], message=message):
        content = event.get("content", {}) if isinstance(event, dict) else {}
        for part in content.get("parts", []):
            if part.get("text"):
                print(part["text"])

    print("=" * 70)


def query_agent(resource_name: str, message: str, user_id: str = "cli_user"):
    """Query a deployed Agent Engine instance"""
    from vertexai import Client

    # Extract project and location from resource name
    # Format: projects/{project}/locations/{location}/reasoningEngines/{id}
    parts = resource_name.split("/")
    project_id = parts[1]
    location = parts[3]

    client = Client(project=project_id, location=location)

    print(f"🤖 Querying agent: {resource_name}")
    print(f"💬 Message: {message}")
    print()

    try:
        asyncio.run(_query_agent_async(client, resource_name, message, user_id))
    except Exception as e:
        print(f"❌ Error querying agent: {str(e)}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


def delete_agent(resource_name: str):
    """Delete a deployed Agent Engine instance"""
    from vertexai import Client

    # Extract project and location from resource name
    parts = resource_name.split("/")
    project_id = parts[1]
    location = parts[3]

    client = Client(project=project_id, location=location)

    print(f"🗑️  Deleting agent: {resource_name}")

    try:
        client.agent_engines.delete(name=resource_name, force=True)
        print("✅ Agent deleted successfully")

    except Exception as e:
        print(f"❌ Error deleting agent: {str(e)}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Manage Agent Engine instances")
    parser.add_argument("command", choices=["list", "query", "delete"], help="Command to execute")
    parser.add_argument(
        "--project",
        default=os.getenv("GOOGLE_CLOUD_PROJECT", "project-ddc15d84-7238-4571-a39"),
        help="Google Cloud project ID",
    )
    parser.add_argument(
        "--location", default=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"), help="Google Cloud location"
    )
    parser.add_argument(
        "--resource-name", default=os.getenv("AGENT_ENGINE_RESOURCE_NAME"), help="Agent Engine resource name"
    )
    parser.add_argument("--message", help="Message to send to the agent (for query command)")
    parser.add_argument("--user-id", default="cli_user", help="User ID for the session")

    args = parser.parse_args()

    if args.command == "list":
        list_agents(args.project, args.location)

    elif args.command == "query":
        if not args.resource_name:
            print("❌ Error: --resource-name is required for query command")
            print("Set AGENT_ENGINE_RESOURCE_NAME or use --resource-name flag")
            sys.exit(1)
        if not args.message:
            print("❌ Error: --message is required for query command")
            sys.exit(1)
        query_agent(args.resource_name, args.message, args.user_id)

    elif args.command == "delete":
        if not args.resource_name:
            print("❌ Error: --resource-name is required for delete command")
            print("Set AGENT_ENGINE_RESOURCE_NAME or use --resource-name flag")
            sys.exit(1)

        # Confirm deletion
        confirm = input(f"Are you sure you want to delete {args.resource_name}? (yes/no): ")
        if confirm.lower() == "yes":
            delete_agent(args.resource_name)
        else:
            print("Deletion cancelled")


if __name__ == "__main__":
    main()
