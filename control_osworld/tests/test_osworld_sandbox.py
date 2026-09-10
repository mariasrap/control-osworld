"""
Integration tests for the OSWorld sandbox environment.

These tests require:
- AWS credentials configured
- Environment variables in .env file:
  - AWS_REGION
  - AWS_SUBNET_ID
  - AWS_SECURITY_GROUP_ID

Run with: pytest -v -s control_osworld/tests/test_osworld_sandbox.py
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Import OSWorld sandbox (requires OSWorld in path)


from control_osworld.osworld_aws_sandbox import (
    OSWorldSandboxConfig,
    OSWorldSandboxEnvironment,
)


def get_instance_state(instance_id: str, region: str) -> str | None:
    """
    Get the current state of an EC2 instance.

    Args:
        instance_id: The EC2 instance ID.
        region: The AWS region.

    Returns:
        Instance state string (e.g., 'running', 'terminated', 'shutting-down'),
        or None if instance not found.
    """
    ec2_client = boto3.client("ec2", region_name=region)
    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        if response["Reservations"]:
            return response["Reservations"][0]["Instances"][0]["State"]["Name"]
        return None
    except ClientError as e:
        if "InvalidInstanceID.NotFound" in str(e):
            return None
        raise


def wait_for_instance_termination(
    instance_id: str, region: str, timeout: int = 300, poll_interval: int = 10
) -> bool:
    """
    Wait for an EC2 instance to be terminated.

    Args:
        instance_id: The EC2 instance ID.
        region: The AWS region.
        timeout: Maximum time to wait in seconds.
        poll_interval: Time between polls in seconds.

    Returns:
        True if instance is terminated/not found, False if timeout reached.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        state = get_instance_state(instance_id, region)
        if state is None or state in ("terminated", "shutting-down"):
            return True
        print(f"Instance {instance_id} state: {state}, waiting...")
        time.sleep(poll_interval)
    return False


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_osworld_sandbox_chrome_navigation():
    """
    Test that the OSWorld sandbox can:
    1. Start an EC2 instance
    2. Open Chrome and navigate to a URL
    3. Take a screenshot
    4. Clean up the instance
    5. Verify the instance is terminated

    This is an integration test that actually creates AWS resources.
    Make sure AWS credentials and environment variables are configured.
    """
    # Verify required environment variables
    required_vars = ["AWS_REGION", "AWS_SUBNET_ID", "AWS_SECURITY_GROUP_ID"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        pytest.skip(f"Missing required environment variables: {missing_vars}")

    region = os.getenv("AWS_REGION", "us-east-1")
    screenshot_path = Path("/tmp/osworld_test_screenshot.png")
    instance_id: str | None = None
    sandbox: OSWorldSandboxEnvironment | None = None

    try:
        # =====================================================================
        # Step 1: Initialize the sandbox (creates EC2 instance)
        # =====================================================================
        print("\n=== Step 1: Initializing OSWorld sandbox ===")

        config = OSWorldSandboxConfig(
            provider_name="aws",
            region=region,
            screen_size=(1920, 1080),
            headless=False,
            require_a11y_tree=False,  # Don't need a11y for this test
            require_terminal=False,
        )

        environments = await OSWorldSandboxEnvironment.sample_init(
            task_name="test_chrome_navigation",
            config=config,
            metadata={},
        )

        sandbox = environments["default"]
        assert isinstance(sandbox, OSWorldSandboxEnvironment)

        instance_id = sandbox.instance_id
        print(f"Created EC2 instance: {instance_id}")
        print(f"VM IP: {sandbox.vm_ip}")

        # Verify instance is running
        state = get_instance_state(instance_id, region)
        assert state == "running", f"Expected instance to be running, got: {state}"
        print(f"Instance state: {state}")
        print("Server is ready (sample_init waits for server to be available)")

        # =====================================================================
        # Step 2: Open Chrome and navigate to drugs.com
        # =====================================================================
        print("\n=== Step 2: Opening Chrome and navigating to drugs.com ===")

        # Launch Chrome using the /setup/launch endpoint (fire-and-forget)
        # This is the same way SetupController launches applications
        launched = await sandbox.launch(
            ["google-chrome", "--no-sandbox", "--disable-gpu", "https://www.drugs.com"]
        )
        print(f"Chrome launch result: {launched}")

        # Wait for the page to load
        print("Waiting for page to load...")
        await asyncio.sleep(10)

        # =====================================================================
        # Step 3: Take a screenshot
        # =====================================================================
        print("\n=== Step 3: Taking screenshot ===")

        screenshot_bytes = await sandbox.get_screenshot()
        assert screenshot_bytes is not None, "Failed to get screenshot"
        assert len(screenshot_bytes) > 0, "Screenshot is empty"

        # Save the screenshot
        screenshot_path.write_bytes(screenshot_bytes)
        print(f"Screenshot saved to: {screenshot_path}")
        print(f"Screenshot size: {len(screenshot_bytes)} bytes")

        # Verify it's a valid PNG (check magic bytes)
        assert screenshot_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Screenshot is not a valid PNG"
        print("Screenshot is a valid PNG image")

        # =====================================================================
        # Step 4: Cleanup the sandbox
        # =====================================================================
        print("\n=== Step 4: Cleaning up sandbox ===")

        await OSWorldSandboxEnvironment.sample_cleanup(
            task_name="test_chrome_navigation",
            config=config,
            environments=environments,
            interrupted=False,
        )
        print("Sandbox cleanup completed")

        # =====================================================================
        # Step 5: Verify instance is terminated
        # =====================================================================
        print("\n=== Step 5: Verifying instance termination ===")

        terminated = wait_for_instance_termination(
            instance_id, region, timeout=120, poll_interval=5
        )
        assert terminated, f"Instance {instance_id} was not terminated within timeout"

        final_state = get_instance_state(instance_id, region)
        print(f"Final instance state: {final_state}")
        assert final_state in (
            None,
            "terminated",
            "shutting-down",
        ), f"Expected instance to be terminated, got: {final_state}"

        print("\n=== Test completed successfully! ===")

    except Exception as e:
        print(f"\n=== Test failed with error: {e} ===")

        # Emergency cleanup: try to terminate the instance if it's still running
        if instance_id:
            print(f"Attempting emergency cleanup of instance {instance_id}...")
            try:
                ec2_client = boto3.client("ec2", region_name=region)
                ec2_client.terminate_instances(InstanceIds=[instance_id])
                print(f"Emergency termination request sent for {instance_id}")
            except Exception as cleanup_error:
                print(f"Emergency cleanup failed: {cleanup_error}")

        raise

    finally:
        # Report screenshot location if it exists
        if screenshot_path.exists():
            print(f"\nScreenshot available at: {screenshot_path}")


@pytest.mark.skip_ci
@pytest.mark.asyncio
async def test_osworld_sandbox_exec_command():
    """
    Test basic command execution in the OSWorld sandbox.

    This test verifies that:
    1. The sandbox can execute simple bash commands
    2. stdout/stderr are captured correctly
    3. Return codes are reported correctly
    """
    # Verify required environment variables
    required_vars = ["AWS_REGION", "AWS_SUBNET_ID", "AWS_SECURITY_GROUP_ID"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        pytest.skip(f"Missing required environment variables: {missing_vars}")

    region = os.getenv("AWS_REGION", "us-east-1")
    instance_id: str | None = None

    try:
        print("\n=== Initializing sandbox for exec test ===")

        config = OSWorldSandboxConfig(
            provider_name="aws",
            region=region,
        )

        environments = await OSWorldSandboxEnvironment.sample_init(
            task_name="test_exec",
            config=config,
            metadata={},
        )

        sandbox = environments["default"]
        instance_id = sandbox.instance_id
        print(f"Created instance: {instance_id}")

        # Test 1: Simple echo command
        print("\n--- Test: echo command ---")
        result = await sandbox.exec(["echo", "Hello, OSWorld!"])
        assert result.success, f"Echo command failed: {result.stderr}"
        assert "Hello, OSWorld!" in result.stdout
        print(f"Echo output: {result.stdout.strip()}")

        # Test 2: Command with exit code
        print("\n--- Test: exit code handling ---")
        result = await sandbox.exec(["bash", "-c", "exit 42"])
        assert not result.success
        assert result.returncode == 42
        print(f"Exit code correctly captured: {result.returncode}")

        # Test 3: Environment variables
        print("\n--- Test: environment variables ---")
        result = await sandbox.exec(
            ["bash", "-c", "echo $MY_VAR"],
            env={"MY_VAR": "test_value_123"},
        )
        assert result.success
        assert "test_value_123" in result.stdout
        print(f"Env var output: {result.stdout.strip()}")

        # Test 4: Working directory
        print("\n--- Test: working directory ---")
        result = await sandbox.exec(["pwd"], cwd="/tmp")
        assert result.success
        assert "/tmp" in result.stdout
        print(f"Working dir output: {result.stdout.strip()}")

        print("\n=== All exec tests passed! ===")

    finally:
        # Cleanup
        if "environments" in locals():
            print("\nCleaning up...")
            await OSWorldSandboxEnvironment.sample_cleanup(
                task_name="test_exec",
                config=config,
                environments=environments,
                interrupted=False,
            )

            if instance_id:
                # Wait briefly for termination
                wait_for_instance_termination(instance_id, region, timeout=60)
                print("Cleanup completed")

