"""
Custom Inspect AI Sandbox Environment wrapping OSWorld's DesktopEnv.

This module provides an Inspect-compatible sandbox that uses OSWorld's DesktopEnv
to manage and interact with EC2 instances running the OSWorld desktop environment.

Why a bespoke sandbox? Inspect / Control Arena's built-in sandboxes (Docker, k8s,
local) cannot drive OSWorld: OSWorld provisions and manages its own EC2 VMs and
talks to them over a Flask control server via ``DesktopEnv``. This class adapts
``DesktopEnv`` to Inspect's ``SandboxEnvironment`` interface (exec / read_file /
write_file / screenshots / lifecycle) so that OSWorld tasks plug into the Control
Arena evaluation pipeline unchanged.

See: https://inspect.aisi.org.uk/extensions.html#sec-sandbox-environment-extensions
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Literal, overload

# Set proxy config path before importing OSWorld, which reads it at module load time.
_PROXY_CONFIG_PATH = Path(__file__).parent / "evaluation_examples" / "settings" / "proxy" / "dataimpulse.json"
if _PROXY_CONFIG_PATH.exists():
    os.environ.setdefault("PROXY_CONFIG_FILE", str(_PROXY_CONFIG_PATH))

import requests
from desktop_env.desktop_env import DesktopEnv
from inspect_ai.util import (
    ExecResult,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
)
from inspect_ai.util._sandbox.registry import sandboxenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Exported for tests
__all__ = ["OSWorldSandboxEnvironment", "OSWorldSandboxConfig"]

# Path to the computer tool compatibility script (relative to this file)
_COMPUTER_TOOL_SCRIPT = Path(__file__).parent / "computer_tool" / "vm_script.py"

# Target path on the VM where Inspect expects the computer tool
_COMPUTER_TOOL_VM_PATH = "/opt/inspect/tool/computer_tool.py"

# Cap for the `type` action text length. Beyond this, pyautogui.typewrite runs so
# long it exceeds the VM's /execute timeout and returns a 500, crashing the task.
# Intercept here so the agent gets a recoverable error instead.
_TYPE_TEXT_MAX_LENGTH = 1000

# Cap for the `wait --duration` value. The VM's /execute endpoint has a 120s
# server-side timeout (see do_request below). A wait that consumes the full window
# leaves no room for the response to be returned, and the server replies 500.
# Rewrite the duration silently; the agent sees the truthful "Waited Ns" output
# and can issue another wait if it needs more.
_WAIT_DURATION_MAX_SECONDS = 100


def _shell_quote_arg(arg: str) -> str:
    """Quote a single argument for shell usage, handling newlines and special chars.

    Unlike shlex.quote, this properly handles newlines and other control characters
    by using printf with command substitution for POSIX compatibility.

    Note: Trailing newlines may be stripped due to shell command substitution behavior.
    """
    if not arg:
        return "''"

    # If no special characters, return as-is
    if re.match(r"^[a-zA-Z0-9_./:@=-]+$", arg):
        return arg

    # If the string contains control characters (newline, carriage return, tab),
    # use printf for POSIX-compliant escaping
    if "\n" in arg or "\r" in arg or "\t" in arg:
        # Escape for printf format string
        escaped = arg
        escaped = escaped.replace("\\", "\\\\")  # backslash -> \\
        escaped = escaped.replace("\n", "\\n")  # newline -> \n
        escaped = escaped.replace("\r", "\\r")  # carriage return -> \r
        escaped = escaped.replace("\t", "\\t")  # tab -> \t
        # Handle single quotes: end quote, add escaped quote, resume quote
        escaped = escaped.replace("'", "'\"'\"'")

        # Use a fixed `%b` format with the text as the ARGUMENT (not the format
        # string). Inspect >=0.3.233 passes `type` as a single `--text=VALUE`
        # token; if VALUE contains a tab/newline it lands here, and feeding it as
        # printf's format made printf parse the leading `--text` as an option
        # ("printf: Illegal option --"). `%b` keeps it as data while still
        # expanding the \n/\r/\t/\\ escapes above; `%` is literal in the argument
        # (so it must NOT be doubled, unlike a format string).
        return "\"$(printf '%b' '" + escaped + "')\""

    # For normal strings without control characters, use shlex.quote
    return shlex.quote(arg)


def _shell_join(cmd: list[str]) -> str:
    """Join a command list into a shell-safe string, handling special characters."""
    return " ".join(_shell_quote_arg(arg) for arg in cmd)


class OSWorldSandboxConfig(BaseModel, frozen=True):
    """Configuration for the OSWorld sandbox environment."""

    def __init__(
        self,
        provider_name: str = "aws",
        region: str | None = None,
        snapshot_name: str = "init_state",
        screen_size: tuple[int, int] = (1920, 1080),
        api_resolution: tuple[int, int] = (1920, 1080),
        post_action_delay: float = 0.5,
        headless: bool = False,
        require_a11y_tree: bool = True,
        require_terminal: bool = False,
        server_startup_timeout: int = 180,
        enable_proxy: bool = False,
    ):
        if region is None:
            region = os.getenv("AWS_REGION")

        super().__init__(
            provider_name=provider_name,
            region=region,
            snapshot_name=snapshot_name,
            screen_size=screen_size,
            api_resolution=api_resolution,
            post_action_delay=post_action_delay,
            headless=headless,
            require_a11y_tree=require_a11y_tree,
            require_terminal=require_terminal,
            server_startup_timeout=server_startup_timeout,
            enable_proxy=enable_proxy,
        )

    provider_name: str = Field(
        default="aws",
        description="The virtualization provider to use. Only 'aws' is supported.",
    )
    region: str | None = Field(
        default=None,
        description="The AWS region to use. Defaults to AWS_REGION env var.",
    )
    snapshot_name: str = Field(
        default="init_state",
        description="The snapshot/AMI name to revert to on reset.",
    )
    screen_size: tuple[int, int] = Field(
        default=(1920, 1080),
        description="The screen resolution of the VM.",
    )
    api_resolution: tuple[int, int] = Field(
        default=(1920, 1080),
        description="The resolution reported to the model API. Screenshots are resized from "
        "screen_size to api_resolution, and model coordinates are scaled back up. "
        "Set to a smaller value (e.g. 1280x720) to match OSWorld's Anthropic scaling.",
    )
    post_action_delay: float = Field(
        default=0.5,
        description="Delay in seconds after each action before taking a screenshot. "
        "OSWorld uses 2.0s. Lower values are faster but may capture stale UI.",
    )
    headless: bool = Field(
        default=False,
        description="Whether to run the VM in headless mode.",
    )
    require_a11y_tree: bool = Field(
        default=True,
        description="Whether to require accessibility tree from the VM.",
    )
    require_terminal: bool = Field(
        default=False,
        description="Whether to require terminal output from the VM.",
    )
    server_startup_timeout: int = Field(
        default=180,
        description="Timeout in seconds to wait for the VM's Flask server to become ready.",
    )
    enable_proxy: bool = Field(
        default=False,
        description="Whether to enable proxy support for tasks that require it.",
    )


@sandboxenv(name="osworld")
class OSWorldSandboxEnvironment(SandboxEnvironment):
    """Inspect AI Sandbox Environment wrapping OSWorld's DesktopEnv.

    This sandbox provides command execution and file operations on an EC2 instance
    running OSWorld's desktop environment. It communicates with the VM via HTTP
    to a Flask server running on the instance.

    The sandbox lifecycle:
    - sample_init: Creates a new DesktopEnv instance (allocates/starts EC2 instance)
    - exec/read_file/write_file: Communicate with the VM via HTTP
    - sample_cleanup: Closes the DesktopEnv (terminates EC2 instance)
    """

    def __init__(self, desktop_env: DesktopEnv):
        """Initialize the sandbox with an existing DesktopEnv instance.

        Args:
            desktop_env: The OSWorld DesktopEnv instance managing the VM.
        """
        self._env = desktop_env
        self._http_server = f"http://{desktop_env.vm_ip}:{desktop_env.server_port}"

    @property
    def env(self) -> DesktopEnv:
        """Access the underlying DesktopEnv instance."""
        return self._env

    @property
    def vm_ip(self) -> str:
        """Get the VM's IP address."""
        return self._env.vm_ip

    @property
    def instance_id(self) -> str | None:
        """Get the EC2 instance ID (path_to_vm in DesktopEnv)."""
        return self._env.path_to_vm

    # =========================================================================
    # OSWorld-specific methods (not part of the SandboxEnvironment interface)
    # =========================================================================

    async def wait_for_server(self, timeout: int = 180, poll_interval: int = 5) -> bool:
        """Wait for the VM's Flask server to become ready.

        After an EC2 instance reaches 'running' state, it takes additional time
        for the OS to boot and the Flask server to start. This method polls
        the server until it responds or times out.

        Args:
            timeout: Maximum time to wait in seconds.
            poll_interval: Time between poll attempts in seconds.

        Returns:
            True if server is ready, False if timeout reached.
        """
        start_time = time.time()
        last_error = None
        attempt = 0

        while time.time() - start_time < timeout:
            attempt += 1
            elapsed = time.time() - start_time
            try:
                # Try a simple GET request to the /platform endpoint
                response = requests.get(
                    f"{self._http_server}/platform",
                    timeout=10,
                )
                if response.status_code == 200:
                    logger.info(
                        f"VM server is ready (took {elapsed:.1f}s, {attempt} attempts)"
                    )
                    return True
            except requests.exceptions.RequestException as e:
                last_error = e
                # Log progress every attempt so user sees activity
                logger.info(
                    f"Waiting for VM server... ({elapsed:.0f}s/{timeout}s, attempt {attempt})"
                )

            await asyncio.sleep(poll_interval)

        logger.error(
            f"VM server not ready after {timeout}s ({attempt} attempts). Last error: {last_error}"
        )
        return False

    async def get_screenshot(self) -> bytes | None:
        """Get a screenshot from the VM.

        Returns:
            PNG image bytes, or None if screenshot failed.
        """

        def do_screenshot() -> bytes | None:
            return self._env.controller.get_screenshot()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, do_screenshot)

    async def get_accessibility_tree(self) -> str | None:
        """Get the accessibility tree from the VM.

        Returns:
            Accessibility tree as XML string, or None if failed.
        """

        def do_get_a11y() -> str | None:
            return self._env.controller.get_accessibility_tree()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, do_get_a11y)

    async def execute_pyautogui(self, command: str) -> dict[str, Any] | None:
        """Execute a PyAutoGUI command on the VM.

        This is useful for GUI automation (clicking, typing, etc.).

        Args:
            command: PyAutoGUI command string (e.g., "pyautogui.click(100, 200)")

        Returns:
            Response dict with status, output, error, or None if failed.
        """

        def do_execute() -> dict[str, Any] | None:
            return self._env.controller.execute_python_command(command)

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, do_execute)

    async def launch(self, command: list[str] | str, shell: bool = False) -> bool:
        """Launch an application on the VM (fire-and-forget).

        This uses the /setup/launch endpoint which starts the process
        and returns immediately without waiting for it to exit.
        Perfect for GUI applications like Chrome, Firefox, etc.

        Args:
            command: Command to launch (list of args or string if shell=True).
            shell: Whether to run via shell.

        Returns:
            True if launch request succeeded, False otherwise.
        """

        def do_launch() -> requests.Response:
            payload = {"command": command, "shell": shell}
            response = requests.post(
                f"{self._http_server}/setup/launch",
                json=payload,
                timeout=30,
            )
            return response

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, do_launch)

            if response.status_code == 200:
                logger.info(f"Launched: {command}")
                return True
            else:
                logger.error(f"Failed to launch {command}: {response.text}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to launch {command}: {e}")
            return False

    async def deploy_computer_tool(
        self,
        screen_size: tuple[int, int],
        api_resolution: tuple[int, int],
        post_action_delay: float,
    ) -> bool:
        """Deploy the Inspect-compatible computer tool script to the VM.

        This copies the computer_tool/vm_script.py script to /opt/inspect/tool/computer_tool.py
        on the VM, enabling Inspect's native computer() tool to work with this sandbox.
        The script's SCREEN_SIZE and API_RESOLUTION constants are replaced with the
        provided values before deployment.

        Requires the CLIENT_PASSWORD environment variable to be set with the sudo password.

        Returns:
            True if deployment succeeded, False otherwise.
        """
        try:
            # Get sudo password from environment
            sudo_password = os.environ.get("CLIENT_PASSWORD")
            if not sudo_password:
                logger.error(
                    "CLIENT_PASSWORD environment variable not set. "
                    "Cannot deploy computer tool without sudo password."
                )
                return False

            # Read the compatibility script
            if not _COMPUTER_TOOL_SCRIPT.exists():
                logger.error(
                    f"Computer tool script not found at {_COMPUTER_TOOL_SCRIPT}"
                )
                return False

            script_content = _COMPUTER_TOOL_SCRIPT.read_text()

            # Inject resolution config into the script
            script_content = script_content.replace(
                "SCREEN_SIZE = (1920, 1080)",
                f"SCREEN_SIZE = {screen_size}",
            )
            script_content = script_content.replace(
                "API_RESOLUTION = (1920, 1080)",
                f"API_RESOLUTION = {api_resolution}",
            )
            script_content = script_content.replace(
                "POST_ACTION_DELAY = 0.5",
                f"POST_ACTION_DELAY = {post_action_delay}",
            )

            # Write the script to a temp location first (doesn't need sudo)
            temp_path = "/tmp/computer_tool.py"  # noqa: S108
            await self.write_file(temp_path, script_content)

            # Helper to run sudo commands with password via stdin
            async def sudo_exec(cmd: str) -> ExecResult[str]:
                # Use echo to pipe password to sudo -S
                # The -S flag makes sudo read password from stdin
                full_cmd = f"echo '{sudo_password}' | sudo -S {cmd}"
                return await self.exec(["bash", "-c", full_cmd], timeout=30)

            # Create the target directory with sudo
            mkdir_result = await sudo_exec("mkdir -p /opt/inspect/tool")
            if not mkdir_result.success:
                logger.error(
                    f"Failed to create /opt/inspect/tool: {mkdir_result.stderr}"
                )
                return False

            # Move the script from temp to target location with sudo
            mv_result = await sudo_exec(f"mv {temp_path} {_COMPUTER_TOOL_VM_PATH}")
            if not mv_result.success:
                logger.error(
                    f"Failed to move script to {_COMPUTER_TOOL_VM_PATH}: {mv_result.stderr}"
                )
                return False

            # Make it executable with sudo
            chmod_result = await sudo_exec(f"chmod +x {_COMPUTER_TOOL_VM_PATH}")
            if not chmod_result.success:
                logger.error(f"Failed to chmod computer_tool.py: {chmod_result.stderr}")
                return False

            # Verify the script is accessible (doesn't need sudo for reading)
            verify_result = await self.exec(
                [
                    "python3",
                    "-c",
                    f"import ast; ast.parse(open('{_COMPUTER_TOOL_VM_PATH}').read())",
                ],
                timeout=30,
            )
            if not verify_result.success:
                logger.error(
                    f"Computer tool script verification failed: {verify_result.stderr}"
                )
                return False

            logger.info(
                f"Successfully deployed computer tool to {_COMPUTER_TOOL_VM_PATH}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to deploy computer tool: {e}")
            return False

    # =========================================================================
    # SandboxEnvironment interface methods
    # =========================================================================

    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] = {},
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        """Execute a command on the OSWorld VM.

        This uses the VM's /execute endpoint with shell=True to run commands.

        Args:
            cmd: Command and arguments to execute.
            input: Standard input (not currently supported).
            cwd: Working directory for command execution.
            env: Environment variables (prepended to command as exports).
            user: User to run as (not currently supported).
            timeout: Execution timeout in seconds (server has 120s max).
            timeout_retry: Whether to retry on timeout (not currently used).
            concurrency: Whether to use concurrency throttling (not currently used).

        Returns:
            ExecResult with stdout, stderr, and return code.
        """
        if input is not None:
            logger.warning(
                "OSWorld sandbox does not support stdin input; ignoring input parameter"
            )

        if user is not None:
            logger.warning("OSWorld sandbox does not support user parameter; ignoring")

        # Guard against overly long `type` actions that crash the VM's /execute endpoint.
        # Inspect may pass the value either as two args (`--text VALUE`) or collapsed
        # into a single `--text=VALUE` arg (Inspect >=0.3.233); handle both forms.
        type_text = None
        if (
            len(cmd) >= 4
            and cmd[0] == "python3"
            and cmd[1].endswith("computer_tool.py")
            and cmd[2] == "type"
        ):
            if cmd[3] == "--text" and len(cmd) >= 5:
                type_text = cmd[4]
            elif cmd[3].startswith("--text="):
                type_text = cmd[3][len("--text="):]
        if type_text is not None and len(type_text) > _TYPE_TEXT_MAX_LENGTH:
            error_message = (
                f"Text too long for type action ({len(type_text)} chars, "
                f"max {_TYPE_TEXT_MAX_LENGTH}). Write the content to a file "
                "and open it, or break the input into smaller chunks."
            )
            return ExecResult(
                success=True,
                returncode=0,
                stdout=json.dumps({"error": error_message}),
                stderr="",
            )

        # Cap `wait --duration` so it can't fill the entire VM /execute timeout
        # window. Rewrite the arg in-place; the server returns "Waited Ns" with
        # the capped value, so the agent learns the true duration.
        if (
            len(cmd) >= 4
            and cmd[0] == "python3"
            and cmd[1].endswith("computer_tool.py")
            and cmd[2] == "wait"
        ):
            cmd = list(cmd)
            for i in range(3, len(cmd) - 1):
                if cmd[i] == "--duration":
                    try:
                        d = int(cmd[i + 1])
                    except (TypeError, ValueError):
                        break
                    if d > _WAIT_DURATION_MAX_SECONDS:
                        cmd[i + 1] = str(_WAIT_DURATION_MAX_SECONDS)
                    break

        # Guard against argparse misinterpreting dash-leading values as flags.
        # When invoking computer_tool.py with `--text VALUE` (or other value-taking
        # flags) where VALUE starts with `-` (e.g. "-0.18cm"), argparse parses
        # VALUE as another option and raises "expected one argument". Collapse
        # `--FLAG VALUE` -> `--FLAG=VALUE` for known value-taking flags so argparse
        # treats VALUE as the option's value regardless of its leading character.
        if (
            len(cmd) >= 4
            and cmd[0] == "python3"
            and cmd[1].endswith("computer_tool.py")
        ):
            value_taking_flags = {
                "--text",
                "--duration",
                "--scroll_amount",
                "--scroll_direction",
            }
            patched_cmd: list[str] = []
            i = 0
            while i < len(cmd):
                if (
                    cmd[i] in value_taking_flags
                    and i + 1 < len(cmd)
                    and cmd[i + 1].startswith("-")
                ):
                    patched_cmd.append(f"{cmd[i]}={cmd[i + 1]}")
                    i += 2
                else:
                    patched_cmd.append(cmd[i])
                    i += 1
            cmd = patched_cmd

        # Build a shell command string
        command_parts = []

        # Add environment variable exports
        for key, value in env.items():
            # Escape single quotes in value
            escaped_value = value.replace("'", "'\"'\"'")
            command_parts.append(f"export {key}='{escaped_value}'")

        # Add cd if working directory specified
        if cwd is not None:
            command_parts.append(f"cd {cwd}")

        # Use custom shell_join for proper escaping of all metacharacters including newlines
        cmd_str = _shell_join(cmd)
        command_parts.append(cmd_str)

        # Join with && so we stop on cd failure but continue after exports
        if len(command_parts) > 1:
            # Exports use ;, cd and command use &&
            if env and cwd:
                exports = "; ".join(command_parts[: len(env)])
                rest = " && ".join(command_parts[len(env) :])
                full_command = f"{exports}; {rest}"
            elif env:
                exports = "; ".join(command_parts[: len(env)])
                full_command = f"{exports}; {cmd_str}"
            elif cwd:
                full_command = " && ".join(command_parts)
            else:
                full_command = cmd_str
        else:
            full_command = cmd_str

        # Server timeout is 120s max
        timeout_seconds = min(timeout if timeout is not None else 120, 120)

        def do_request() -> dict[str, Any]:
            payload = {
                "command": full_command,
                "shell": True,
            }
            response = requests.post(
                f"{self._http_server}/execute",
                json=payload,
                timeout=timeout_seconds + 60,  # HTTP timeout buffer
            )
            response.raise_for_status()
            return response.json()

        try:
            # Run the synchronous request in a thread pool to not block the event loop
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, do_request)

            stdout = result.get("output", "")
            stderr = result.get("error", "")
            returncode = result.get("returncode", -1)

            return ExecResult(
                success=returncode == 0,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )

        except requests.exceptions.Timeout:
            raise TimeoutError(
                f"Command execution timed out after {timeout_seconds} seconds"
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to execute command on VM: {e}")
            return ExecResult(
                success=False,
                returncode=-1,
                stdout="",
                stderr=f"Failed to communicate with VM: {e}",
            )

    async def write_file(self, file: str, contents: str | bytes) -> None:
        """Write a file to the OSWorld VM.

        Uses the VM's /setup/upload endpoint.

        Args:
            file: Path to the file on the VM.
            contents: File contents (text or binary).

        Raises:
            PermissionError: If the file cannot be written.
            IsADirectoryError: If the path is a directory.
        """
        if isinstance(contents, str):
            contents = contents.encode("utf-8")

        def do_upload() -> requests.Response:
            files = {"file_data": ("file", io.BytesIO(contents))}
            data = {"file_path": file}
            response = requests.post(
                f"{self._http_server}/setup/upload",
                files=files,
                data=data,
                timeout=60,
            )
            return response

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, do_upload)

            if response.status_code != 200:
                error_msg = response.text
                if "Permission denied" in error_msg:
                    raise PermissionError(f"Cannot write to {file}: {error_msg}")
                if "Is a directory" in error_msg:
                    raise IsADirectoryError(f"Path is a directory: {file}")
                raise IOError(f"Failed to write file {file}: {error_msg}")

        except requests.exceptions.RequestException as e:
            raise IOError(f"Failed to communicate with VM while writing {file}: {e}")

    @overload
    async def read_file(self, file: str, text: Literal[True] = True) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False]) -> bytes: ...

    async def read_file(self, file: str, text: bool = True) -> str | bytes:
        """Read a file from the OSWorld VM.

        Uses the VM's /file endpoint.

        Args:
            file: Path to the file on the VM.
            text: Whether to return text (True) or bytes (False).

        Returns:
            File contents as string or bytes.

        Raises:
            FileNotFoundError: If the file does not exist.
            PermissionError: If the file cannot be read.
            IsADirectoryError: If the path is a directory.
        """

        def do_download() -> requests.Response:
            response = requests.post(
                f"{self._http_server}/file",
                data={"file_path": file},
                timeout=60,
            )
            return response

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, do_download)

            if response.status_code == 404:
                raise FileNotFoundError(f"File not found: {file}")

            if response.status_code != 200:
                error_msg = response.text
                if "Permission denied" in error_msg:
                    raise PermissionError(f"Cannot read {file}: {error_msg}")
                if "Is a directory" in error_msg:
                    raise IsADirectoryError(f"Path is a directory: {file}")
                raise IOError(f"Failed to read file {file}: {error_msg}")

            if text:
                # Preserve newlines as-is (don't convert crlf to lf)
                return response.content.decode("utf-8")
            else:
                return response.content

        except requests.exceptions.RequestException as e:
            raise IOError(f"Failed to communicate with VM while reading {file}: {e}")

    @classmethod
    async def sample_init(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        metadata: dict[str, str],
    ) -> dict[str, SandboxEnvironment]:
        """Initialize sandbox environment(s) for a sample.

        This creates a new DesktopEnv instance, which will allocate or start
        an EC2 instance.

        Args:
            task_name: Name of the task.
            config: Sandbox configuration (OSWorldSandboxConfig or dict).
            metadata: Sample metadata.

        Returns:
            Dictionary with "default" key pointing to the sandbox instance.
        """
        # Parse configuration
        if config is None:
            sandbox_config = OSWorldSandboxConfig()
        elif isinstance(config, dict):
            sandbox_config = OSWorldSandboxConfig(**config)
        elif isinstance(config, OSWorldSandboxConfig):
            sandbox_config = config
        else:
            raise ValueError(f"Unexpected config type: {type(config)}")

        logger.info(
            f"Initializing OSWorld sandbox for task '{task_name}' "
            f"with provider '{sandbox_config.provider_name}'"
        )

        # Extract config values with explicit types
        provider_name: str = sandbox_config.provider_name
        region: str | None = sandbox_config.region
        snapshot_name: str = sandbox_config.snapshot_name
        screen_size: tuple[int, int] = sandbox_config.screen_size
        api_resolution: tuple[int, int] = sandbox_config.api_resolution
        post_action_delay: float = sandbox_config.post_action_delay
        headless: bool = sandbox_config.headless
        require_a11y_tree: bool = sandbox_config.require_a11y_tree
        require_terminal: bool = sandbox_config.require_terminal
        server_startup_timeout: int = sandbox_config.server_startup_timeout
        enable_proxy: bool = sandbox_config.enable_proxy

        # Create DesktopEnv in a thread pool to avoid blocking the event loop.
        # Note: OSWorld's AWS provider signal handlers are disabled here (not main thread).
        # Signal handling is done by inspect_ai, which calls sample_cleanup on interruption.
        # If Ctrl-C happens during instance creation, the TTL scheduler is the safety net.
        def create_env() -> DesktopEnv:
            return DesktopEnv(
                provider_name=provider_name,
                region=region,
                snapshot_name=snapshot_name,
                headless=headless,
                require_a11y_tree=require_a11y_tree,
                require_terminal=require_terminal,
                enable_proxy=enable_proxy,
            )

        loop = asyncio.get_event_loop()
        desktop_env = await loop.run_in_executor(None, create_env)

        sandbox = cls(desktop_env)

        # Wait for the VM's Flask server to become ready.
        # The EC2 instance may be "running" but the server needs time to start.
        logger.info(
            f"Waiting for VM server to become ready (timeout: {server_startup_timeout}s)..."
        )
        server_ready = await sandbox.wait_for_server(timeout=server_startup_timeout)
        if not server_ready:
            # Clean up the instance if server never became ready
            logger.error("VM server did not become ready, cleaning up instance...")
            try:
                desktop_env.close()
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
            raise RuntimeError(
                f"VM server at {sandbox._http_server} did not become ready "
                f"within {server_startup_timeout} seconds"
            )

        # Deploy the Inspect-compatible computer tool script to the VM.
        # This enables Inspect's native computer() tool to work with this sandbox,
        # which in turn allows automatic conversion to OpenAI CUA / Anthropic native formats.
        logger.info("Deploying Inspect computer tool compatibility script to VM...")
        tool_deployed = await sandbox.deploy_computer_tool(
            screen_size=screen_size,
            api_resolution=api_resolution,
            post_action_delay=post_action_delay,
        )
        if not tool_deployed:
            logger.warning(
                "Failed to deploy computer tool script. "
                "Inspect's native computer() tool may not work correctly."
            )

        return {"default": sandbox}

    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        """Cleanup sandbox environments for a sample.

        This closes the DesktopEnv, which will terminate the EC2 instance.

        Args:
            task_name: Name of the task.
            config: Sandbox configuration.
            environments: Dictionary of sandbox environments to cleanup.
            interrupted: Whether the task was interrupted.
        """
        for name, sandbox in environments.items():
            if isinstance(sandbox, OSWorldSandboxEnvironment):
                logger.info(
                    f"Cleaning up OSWorld sandbox '{name}' for task '{task_name}'"
                    + (" (interrupted)" if interrupted else "")
                )

                def close_env(env: DesktopEnv) -> None:
                    try:
                        env.close()
                    except Exception as e:
                        logger.error(f"Error closing DesktopEnv: {e}")

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, close_env, sandbox._env)

    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        """Deserialize sandbox configuration from a dictionary."""
        return OSWorldSandboxConfig(**config)
