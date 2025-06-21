
import click
import llm # Main LLM library
import subprocess # For running git and gh commands
from prompt_toolkit import PromptSession # For interactive editing
from prompt_toolkit.patch_stdout import patch_stdout # Important for prompt_toolkit
from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
from prompt_toolkit.shortcuts import print_formatted_text, confirm as pt_confirm # For chat UI and confirmations
from prompt_toolkit.styles import Style
from prompt_toolkit.key_binding import KeyBindings
import os
import json
import tempfile
import asyncio # For async chat refinement

# --- Configuration Management ---
CONFIG_DIR = click.get_app_dir("llm-gh-pr")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_MAX_CHARS_COMMITS = 10000
DEFAULT_MAX_CHARS_DIFF = 15000

def load_config():
    """Loads configuration from the JSON file."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def save_config(config_data):
    """Saves configuration to the JSON file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config_data, f, indent=2)

# --- System Prompts ---
DEFAULT_PR_SYSTEM_PROMPT = """
You are an expert software developer tasked with writing a clear, concise, and informative GitHub Pull Request.
You will be provided with a list of commit messages and optionally a git diff from the branch to be merged.
Your goal is to create a PR title and a PR body.

**Output Format (STRICT):**
You MUST output the PR title on the first line, followed by "---PR_BODY_SEPARATOR---", and then the PR body.
The PR body should be well-formatted markdown.

Example:
feat: Implement user authentication service
---PR_BODY_SEPARATOR---
This pull request introduces the new user authentication service.

**Key Changes:**
- Added `AuthService` for handling user login and registration.
- Integrated with the existing user database.
- Includes unit tests for all new endpoints.

**Motivation:**
To provide a secure way for users to access the application.

**How to Test:**
1. Run the application.
2. Attempt to register a new user via the `/register` endpoint.
3. Attempt to log in with the new credentials via the `/login` endpoint.

**Related Issues (if any):**
Closes #123

**PR Title Guidelines:**
- Follow conventional commit style (e.g., `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`).
- Be concise, ideally 50-70 characters.
- Summarize the main purpose of the PR.

**PR Body Guidelines:**
- Start with a brief overview of the changes.
- Use markdown for formatting (headings, bullet points, code blocks).
- Clearly explain the "what" and "why" of the changes.
- If applicable, include steps on "how to test" the changes.
- If applicable, mention any related issues (e.g., "Closes #issue_number").
- If the branch contains many small commits, synthesize them into a coherent narrative.
- Focus ONLY on the changes presented. Do not invent features or describe unrelated parts of the project.
- If new files were added, describe their collective purpose or the feature they enable.
"""

PROPOSED_PR_BODY_MARKER_START = "PROPOSED_PR_BODY_START"
PROPOSED_PR_BODY_MARKER_END = "PROPOSED_PR_BODY_END"

CHAT_REFINEMENT_PR_BODY_SYSTEM_PROMPT_TEMPLATE = """
You are an expert AI programmer specializing in refining GitHub Pull Request bodies.
The user will provide their current draft of the PR body, and you have access to the original context (commit list/diff) that informed it.
Your goal is to help the user improve the clarity, completeness, and formatting of their PR body through interactive dialogue.

**Context Provided:**
1.  **Original Context (Commits/Diff):**
    --- CONTEXT START ---
    {original_context_for_llm}
    --- CONTEXT END ---
2.  **User's Current Working Draft of PR Body:** (This draft will evolve if you make proposals that are accepted by the user)
    --- CURRENT DRAFT START ---
    {current_draft_for_llm_context}
    --- CURRENT DRAFT END ---

**Interaction Protocol:**
1.  **Analyze User Input:** Carefully consider the user's queries, requests for changes, or questions.
2.  **Conversational Interaction:** Respond naturally. Provide explanations or ask clarifying questions.
3.  **Proposing Revisions to the PR Body:**
    *   When you are ready to propose a new version of the *entire PR body*, structure your response:
        a.  **Conversational Part:** Explain how you've addressed their request.
        b.  **Structured Proposal Block (Mandatory):** Following your conversational text, you **MUST** provide the **raw PR body markdown** clearly demarcated:
            ```
            PROPOSED_PR_BODY_START
            <new PR body content in markdown>
            PROPOSED_PR_BODY_END
            ```
            The content between these markers **MUST strictly be the markdown for the PR body.**

4.  **Answering Questions / General Discussion:**
    *   If *only* answering a question or discussing parts *without the user asking for a modification to the entire PR body*, **DO NOT use the `PROPOSED_PR_BODY_START`/`END` markers.**

Prioritize user requests. Adhere to good PR body practices (markdown, clarity, what/why/how-to-test).
The system will use the text between the markers for user confirmation.
"""

# --- LLM Plugin Hook ---
@llm.hookimpl
def register_commands(cli):
    @cli.group(name="gh-pr", invoke_without_command=True)
    @click.pass_context
    @click.option(
        "-t", "--target-branch", default=None,
        help="The branch to merge into (e.g., main, develop). Attempts to auto-detect if not provided."
    )
    @click.option(
        "-s", "--source-branch", default=None,
        help="The branch to merge from (your feature branch). Defaults to current branch."
    )
    @click.option(
        "--title", "provide_title", default=None, help="Provide PR title directly, skipping LLM generation for it."
    )
    @click.option(
        "--body", "provide_body", default=None, help="Provide PR body directly, skipping LLM generation for it."
    )
    @click.option(
        "-m", "--model", "model_id_override", default=None,
        help="Specify the LLM model to use."
    )
    @click.option(
        "--system", "system_prompt_override", default=None,
        help="Custom system prompt for PR generation."
    )
    @click.option(
        "--max-commits-chars", "max_commits_chars_override", type=int,
        help=f"Max characters from commit logs to send to LLM (default: {DEFAULT_MAX_CHARS_COMMITS})."
    )
    @click.option(
        "--max-diff-chars", "max_diff_chars_override", type=int,
        help=f"Max characters from git diff to send to LLM (default: {DEFAULT_MAX_CHARS_DIFF})."
    )
    @click.option(
        "--include-diff", is_flag=True, default=False,
        help="Include git diff along with commit messages as context for the LLM."
    )
    @click.option(
        "--draft", is_flag=True, default=False, help="Create the PR as a draft."
    )
    @click.option(
        "-y", "--yes", is_flag=True,
        help="Automatically create the PR with LLM suggestions without interactive editing (still asks for final confirmation)."
    )
    def gh_pr_command(
        ctx, target_branch, source_branch,
        provide_title, provide_body, # these are from click options
        model_id_override, system_prompt_override, # from click options
        max_commits_chars_override, max_diff_chars_override, include_diff, # from click options
        draft, yes # from click options
    ):
        """Generates GitHub PR titles/bodies using an LLM and creates PRs via 'gh' CLI."""
        if ctx.invoked_subcommand is not None:
            return

        config = load_config() # Defined above

        if not _is_git_repository():
            click.echo(click.style("Error: Not inside a git repository.", fg="red"), err=True)
            return

        if not _is_gh_installed():
            click.echo(click.style("Error: GitHub CLI 'gh' not found. Please install it and authenticate (`gh auth login`).", fg="red"), err=True)
            click.echo("See: https://cli.github.com/", err=True)
            return

        repo_details_str = _get_repo_details_gh()
        if not repo_details_str:
            click.echo(click.style("Error: Could not determine GitHub repository owner/name using 'gh'.", fg="red"), err=True)
            click.echo("Ensure 'gh' is installed, authenticated, and you are in a repo connected to GitHub ('gh repo view' should work).", err=True)
            return
        repo_owner, repo_name = repo_details_str.split('/')
        repo_slug = f"{repo_owner}/{repo_name}"

        current_branch_name = _get_current_branch()
        if not current_branch_name:
            click.echo(click.style("Error: Could not determine current git branch.", fg="red"), err=True); return

        actual_source_branch = source_branch or current_branch_name
        actual_target_branch = target_branch or _get_default_branch_gh(repo_slug) or config.get("default_target_branch") or "main"

        if actual_source_branch == actual_target_branch:
            click.echo(click.style(f"Error: Source ({actual_source_branch}) and target ({actual_target_branch}) branches are the same.", fg="red"), err=True); return

        click.echo(f"Preparing PR from {click.style(actual_source_branch, bold=True)} to {click.style(actual_target_branch, bold=True)} for repo {click.style(repo_slug, bold=True)}")

        if not _check_branch_pushed(actual_source_branch):
            push_prompt = f"Source branch '{actual_source_branch}' may not be pushed or up-to-date with its remote tracking branch. Push it now (git push -u origin {actual_source_branch})?"
            if click.confirm(push_prompt, default=True):
                if not _run_git_command(["git", "push", "--set-upstream", "origin", actual_source_branch], "Error pushing source branch.", suppress_output=False, check=True):
                    return
                click.echo(click.style(f"Branch '{actual_source_branch}' pushed.", fg="green"))
            else:
                click.echo("PR creation aborted. Please push your source branch first."); return

        commit_log_str = _get_commits_for_pr(actual_target_branch, actual_source_branch)
        if not commit_log_str.strip():
            click.echo(click.style(f"No new commits found on '{actual_source_branch}' compared to '{actual_target_branch}'.", fg="yellow"))
            if not include_diff and not click.confirm("No commits to list. Proceed anyway (e.g., if only a diff is relevant or you'll write the body manually)?", default=False):
                return
            commit_log_str = "No new commits to list for this PR. Please describe the changes based on the diff if provided, or the general purpose of the PR."

        llm_context_str = f"Commit messages from branch '{actual_source_branch}' (against '{actual_target_branch}'):\n{commit_log_str}" # llm_context_str defined here
        max_commits = max_commits_chars_override or config.get("max-commits-chars") or DEFAULT_MAX_CHARS_COMMITS
        if len(llm_context_str) > max_commits:
            click.echo(click.style(f"Warning: Commit log truncated to {max_commits} characters for LLM.", fg="yellow"))
            llm_context_str = llm_context_str[:max_commits] + "\n\n... [commit log truncated]"

        diff_str_for_llm_context = "" # Variable to hold the diff string if included
        if include_diff:
            raw_diff = _get_diff_for_pr(actual_target_branch, actual_source_branch)
            if raw_diff:
                max_diff = max_diff_chars_override or config.get("max-diff-chars") or DEFAULT_MAX_CHARS_DIFF
                if len(raw_diff) > max_diff:
                    click.echo(click.style(f"Warning: Diff truncated to {max_diff} characters for LLM.", fg="yellow"))
                    diff_str_for_llm_context = raw_diff[:max_diff] + "\n\n... [diff truncated]"
                else:
                    diff_str_for_llm_context = raw_diff
                llm_context_str += f"\n\nGit Diff:\n{diff_str_for_llm_context}" # Add to the main context
            else:
                 click.echo(click.style("No diff found between branches to include.", fg="yellow"))

        generated_title, generated_body = "", ""
        model_obj = None # Initialize model_obj

        if provide_title and provide_body: # check options from click
            generated_title, generated_body = provide_title, provide_body
            click.echo("Using provided title and body.")
        else:
            from llm.cli import get_default_model # Import here

            configured_model_id = config.get("model")
            actual_model_id_for_llm = model_id_override or configured_model_id or get_default_model() # actual_model_id_for_llm defined here
            if not actual_model_id_for_llm:
                click.echo(click.style("Error: No LLM model specified or configured.", fg="red"), err=True); return

            try:
                model_obj = llm.get_model(actual_model_id_for_llm) # model_obj defined here
            except llm.UnknownModelError:
                click.echo(click.style(f"Error: Model '{actual_model_id_for_llm}' not recognized.", fg="red"), err=True); return

            if model_obj.needs_key:
                key_name_for_llm = model_obj.needs_key
                api_key = llm.get_key("", key_name_for_llm, model_obj.key_env_var)
                if not api_key:
                    click.echo(click.style(f"Error: API key for model '{actual_model_id_for_llm}' (key name: {key_name_for_llm}) not found.", fg="red"), err=True)
                    click.echo(f"Try: `llm keys set {key_name_for_llm}` or set ${model_obj.key_env_var}.", err=True)
                    return
                model_obj.key = api_key

            system_prompt_to_use = system_prompt_override or config.get("system_pr_generation") or DEFAULT_PR_SYSTEM_PROMPT # system_prompt_override from click
            click.echo(f"Generating PR details using {click.style(actual_model_id_for_llm, bold=True)}...")

            try:
                response_obj = model_obj.prompt(llm_context_str, system=system_prompt_to_use)
                full_response_text = response_obj.text().strip()
                separator = "---PR_BODY_SEPARATOR---"
                if separator in full_response_text:
                    title_part, body_part = full_response_text.split(separator, 1)
                    generated_title, generated_body = title_part.strip(), body_part.strip()
                else:
                    click.echo(click.style("Warning: LLM output did not use '---PR_BODY_SEPARATOR---'. Assuming first line is title, rest is body.", fg="yellow"))
                    lines = full_response_text.splitlines()
                    generated_title = lines[0].strip() if lines else "Error: Could not generate title"
                    generated_body = "\n".join(lines[1:]).strip() if len(lines) > 1 else "Error: Could not generate body"
                
                if provide_title: generated_title = provide_title # Override if user provided
                if provide_body: generated_body = provide_body   # Override if user provided

            except Exception as e:
                click.echo(click.style(f"Error calling LLM: {e}", fg="red"), err=True); return

        final_title, final_body = generated_title, generated_body

        if not yes: # yes from click
            if not provide_title:
                final_title = _interactive_edit_title(generated_title)
                if final_title is None: click.echo("PR creation aborted."); return
            
            if not provide_body:
                final_body = _interactive_edit_body(generated_body, llm_context_str, model_obj) # llm_context_str and model_obj defined
                if final_body is None: click.echo("PR creation aborted."); return
        else:
            click.echo(click.style("\nUsing LLM-generated PR details directly:", fg="cyan"))
            click.echo(f"Title: {final_title}\nBody:\n{final_body}")

        if not final_title.strip():
            click.echo(click.style("Error: PR Title cannot be empty.", fg="red"), err=True); return

        click.echo(click.style("\n--- Proposed PR ---", bold=True))
        click.echo(f"Repository:    {click.style(repo_slug, fg='blue')}") # repo_slug defined
        click.echo(f"Target Branch: {click.style(actual_target_branch, fg='green')}") # actual_target_branch defined
        click.echo(f"Source Branch: {click.style(actual_source_branch, fg='green')}") # actual_source_branch defined
        click.echo(f"Title:         {click.style(final_title, fg='yellow')}")
        click.echo(f"Body:\n{click.style(final_body, fg='yellow')}")
        if draft: click.echo(click.style("This will be a DRAFT PR.", fg="magenta")) # draft from click

        if click.confirm(f"\nCreate this Pull Request using 'gh cli'?", default=True):
            _create_github_pr_gh(repo_slug, actual_target_branch, actual_source_branch, final_title, final_body, draft) # draft from click
        else:
            click.echo("PR creation aborted by user.")


    @gh_pr_command.command(name="config")
    @click.option("--view", is_flag=True, help="View current config.")
    @click.option("--reset", is_flag=True, help="Reset all configs.")
    @click.option("-m", "--model", "model_config", help="Set default LLM model.")
    @click.option("-s", "--system-pr-generation", "system_pr_config", help="Set default system prompt for PR generation.")
    @click.option("--default-target-branch", "default_target_config", help="Set default target branch (e.g., main).")
    @click.option("--max-commits-chars", "mcc_config", type=int, help="Default max chars for commit logs.")
    @click.option("--max-diff-chars", "mdc_config", type=int, help="Default max chars for git diff.")
    @click.pass_context
    def config_command(ctx, view, reset, model_config, system_pr_config, default_target_config, mcc_config, mdc_config):
        config_data = load_config()
        if view:
            click.echo(f"Config file: {CONFIG_FILE}\n{json.dumps(config_data, indent=2) if config_data else 'No config set.'}"); return
        if reset:
            if click.confirm("Reset all llm-gh-pr configurations?"):
                if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
                click.echo("Config reset.")
            else: click.echo("Reset cancelled.")
            return
        
        updated = False
        if model_config is not None: config_data["model"] = model_config; updated = True; click.echo(f"Model set: {model_config}")
        if system_pr_config is not None: config_data["system_pr_generation"] = system_pr_config; updated = True; click.echo("System prompt updated.")
        if default_target_config is not None: config_data["default_target_branch"] = default_target_config; updated = True; click.echo(f"Default target branch set: {default_target_config}")
        if mcc_config is not None: config_data["max-commits-chars"] = mcc_config; updated = True; click.echo(f"Max commits chars set: {mcc_config}")
        if mdc_config is not None: config_data["max-diff-chars"] = mdc_config; updated = True; click.echo(f"Max diff chars set: {mdc_config}")

        if updated: save_config(config_data); click.echo("Configuration saved.")
        else: click.echo(ctx.get_help())

# --- Helper Functions ---
def _run_git_command(command_parts, error_message="Error executing git command", check=True, capture_output=True, text=True, cwd=".", encoding="utf-8", errors="ignore", suppress_output=False):
    try:
        process = subprocess.run(command_parts, check=check, capture_output=capture_output, text=text, cwd=cwd, encoding=encoding, errors=errors)
        return process.stdout.strip() if capture_output else True
    except subprocess.CalledProcessError as e:
        if not suppress_output:
            err_output = e.stderr.strip() if e.stderr else e.stdout.strip() if e.stdout else str(e)
            click.echo(click.style(f"{error_message}:\n{err_output}", fg="red"), err=True)
        return None
    except FileNotFoundError:
        if not suppress_output:
            click.echo(click.style(f"Error: Command '{command_parts[0]}' not found.", fg="red"), err=True)
        return None

def _is_git_repository():
    return _run_git_command(["git", "rev-parse", "--is-inside-work-tree"], suppress_output=True) == "true"

def _get_current_branch():
    return _run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], "Failed to get current branch", suppress_output=True)

def _is_gh_installed():
    return _run_git_command(["gh", "--version"], suppress_output=True, check=False) is not None

def _get_repo_details_gh():
    command_gh = ["gh", "repo", "view", "--json", "owner,name", "--jq", ".owner.login + \"/\" + .name"]
    details = _run_git_command(command_gh, "Failed to get repo details with gh (context-aware)", suppress_output=True, check=False)
    if details and '/' in details:
        return details

    git_remote_url = _run_git_command(["git", "remote", "get-url", "origin"], "Failed to get remote origin URL", suppress_output=True, check=False)
    if git_remote_url:
        path_part = None
        if "github.com/" in git_remote_url:
            path_part = git_remote_url.split("github.com/", 1)[1]
        elif "github.com:" in git_remote_url:
            path_part = git_remote_url.split("github.com:", 1)[1]
        
        if path_part:
            owner_repo = path_part.replace(".git", "")
            if '/' in owner_repo:
                return owner_repo
    return None

def _get_default_branch_gh(repo_slug):
    if not repo_slug: return None
    command = ["gh", "repo", "view", repo_slug, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    return _run_git_command(command, f"Failed to get default branch for {repo_slug} with gh", suppress_output=True, check=False)

def _check_branch_pushed(branch_name):
    local_commit = _run_git_command(["git", "rev-parse", branch_name], suppress_output=True)
    if not local_commit: return False

    remote_tracking_branch = _run_git_command(["git", "rev-parse", "--symbolic-full-name", f"{branch_name}@{{u}}"], suppress_output=True, check=False)
    if not remote_tracking_branch:
        click.echo(click.style(f"Warning: Branch '{branch_name}' has no remote tracking branch.", fg="yellow"))
        return False

    remote_commit = _run_git_command(["git", "rev-parse", remote_tracking_branch], suppress_output=True, check=False)
    if not remote_commit:
        click.echo(click.style(f"Warning: Remote branch '{remote_tracking_branch}' not found.", fg="yellow"))
        return False

    if local_commit == remote_commit:
        return True
    
    merge_base = _run_git_command(["git", "merge-base", local_commit, remote_commit], suppress_output=True)
    if merge_base == remote_commit:
        return True # Local is ahead
    
    click.echo(click.style(f"Warning: Branch '{branch_name}' is not in sync with '{remote_tracking_branch}'.", fg="yellow"))
    return False

def _get_commits_for_pr(target_branch, source_branch):
    if not _run_git_command(["git", "show-ref", "--verify", f"refs/remotes/origin/{target_branch}"], suppress_output=True, check=False):
        click.echo(click.style(f"Warning: Remote target branch 'origin/{target_branch}' not found. Fetching...", fg="yellow"))
        _run_git_command(["git", "fetch", "origin", target_branch], f"Failed to fetch {target_branch}", suppress_output=True, check=False)

    comparison_range = f"origin/{target_branch}..{source_branch}"
    command = ["git", "log", comparison_range, "--no-merges", "--pretty=format:- %s (%an) [%h]"]
    commits = _run_git_command(command, f"Failed to get commit log for {comparison_range}")
    return commits if commits else ""

def _get_diff_for_pr(target_branch, source_branch):
    comparison_range = f"origin/{target_branch}...{source_branch}"
    diff = _run_git_command(["git", "diff", comparison_range], f"Failed to get git diff for {comparison_range}")
    return diff if diff else ""

def _interactive_edit_title(suggestion: str):
    click.echo(click.style("\nSuggested PR Title (edit, Enter to submit, Ctrl+C/D to cancel):", fg="cyan"))
    session = PromptSession(message=to_formatted_text("PR Title: ", style="fg:ansiblue bold")) # Ensure to_formatted_text
    try:
        with patch_stdout():
            edited_title = session.prompt(default=suggestion)
        return edited_title
    except (KeyboardInterrupt, EOFError):
        return None

def _interactive_edit_body(suggestion: str, original_context_for_llm: str, model_obj: llm.Model = None):
    click.echo(click.style("\nSuggested PR Body (edit below):", fg="cyan"))
    prompt_instructions_text = """\
Type/edit your PR body.
  - To add a NEW LINE: Press Enter.
  - To SUBMIT message: Press Esc, then press Enter (or Alt+Enter/Option+Enter).
  - Chat to Refine PR Body (if LLM available): Ctrl+I.
  - To CANCEL: Press Ctrl+D or Ctrl-C.

PR Body:
"""
    custom_style = Style.from_dict({ # custom_style defined here
        'instruction': 'ansicyan',
        'chat-user-prompt': 'fg:ansimagenta bold',
        'chat-user-text': 'fg:ansiwhite',
        'chat-llm-prompt': 'bold fg:ansigreen',
        'chat-llm-text': '',
        'chat-info': 'fg:ansiblue',
        'chat-highlight': 'bold fg:ansiyellow',
        'chat-separator': 'fg:ansibrightblack',
        'chat-toolbar': 'fg:ansiblack bg:ansicyan',
        'chat-toolbar-key': 'bold',
    })

    kb_main_editor = KeyBindings() # kb_main_editor defined
    if model_obj:
        @kb_main_editor.add('c-i') # Added to kb_main_editor
        async def _handle_chat_refine_body_kb(event):
            app_for_chat = event.app
            current_text_in_editor = app_for_chat.current_buffer.text
            
            with patch_stdout(): # Ensure printing outside main prompt is fine
                print_formatted_text(FormattedText([('bold fg:ansimagenta', "\n==> Entering Chat Mode for PR Body...")]))
            
            refined_body_from_chat = await _chat_for_pr_body_refinement(
                initial_draft=current_text_in_editor,
                original_ctx=original_context_for_llm, # original_context_for_llm passed in
                model=model_obj, # model_obj passed in
                passed_style=custom_style, # custom_style defined above
                pt_app_for_printing=app_for_chat 
            )

            with patch_stdout():
                print_formatted_text(FormattedText([('bold fg:ansimagenta', "<== Exiting Chat Mode...")]))

            if refined_body_from_chat is not None:
                if refined_body_from_chat != current_text_in_editor:
                    app_for_chat.current_buffer.text = refined_body_from_chat
                    app_for_chat.current_buffer.cursor_position = len(refined_body_from_chat)
            
            app_for_chat.invalidate()

    session = PromptSession(
        message=FormattedText([('class:instruction', prompt_instructions_text)]),
        style=custom_style,
        key_bindings=kb_main_editor, # Use kb_main_editor
        multiline=True,
    )
    
    edited_body = None
    try:
        with patch_stdout():
            edited_body = session.prompt(default=suggestion)
        return edited_body
    except (KeyboardInterrupt, EOFError):
        return None

async def _chat_for_pr_body_refinement(initial_draft: str, original_ctx: str, model: llm.Model, passed_style: Style, pt_app_for_printing=None) -> str:
    """Handles interactive chat for refining PR bodies."""

    # Using a local print_styled function that respects patch_stdout for the chat's own prompt session
    def print_styled_chat(text_parts_tuples, end='\n'):
        with patch_stdout(): # Ensure this print doesn't interfere with the live chat prompt
            print_formatted_text(FormattedText(text_parts_tuples), style=passed_style, end=end)

    print_styled_chat([('bold fg:ansimagenta', "\n--- PR Body Chat Session ---")])
    print_styled_chat([('class:chat-info', "LLM considers original commits/diff & the initial draft.")])
    print_styled_chat([('class:chat-highlight', f"\nInitial Draft (when chat started):")])
    for line in initial_draft.splitlines(): print_styled_chat([('class:instruction', line)])
    print_styled_chat([('class:chat-separator', "---")])

    chat_history = []
    message_being_refined_in_chat = initial_draft

    def get_current_chat_system_prompt():
        return CHAT_REFINEMENT_PR_BODY_SYSTEM_PROMPT_TEMPLATE.format(
            original_context_for_llm=original_ctx,
            current_draft_for_llm_context=message_being_refined_in_chat
        )
    
    def get_chat_bottom_toolbar_ft():
        return FormattedText([
            ('class:chat-toolbar', "[Chat] "),
            ('class:chat-toolbar class:chat-toolbar-key', "Ctrl+A"), ('class:chat-toolbar', " or "),
            ('class:chat-toolbar class:chat-toolbar-key', "/apply"), ('class:chat-toolbar', ": Use Current Draft & Exit | "),
            ('class:chat-toolbar class:chat-toolbar-key', "/cancel"), ('class:chat-toolbar', ": Discard Chat & Exit"),
        ])

    chat_kb_for_session = KeyBindings() # chat_kb_for_session defined
    @chat_kb_for_session.add('c-a')
    async def _handle_apply_chat_via_ctrl_a(event):
        print_styled_chat([('fg:ansicyan', "\n(Ctrl+A pressed, applying current chat draft...)")])
        event.app.exit(result="/apply")



    # Use passed_style directly, as it contains all necessary definitions
    chat_session_style_to_use = passed_style

    chat_session = PromptSession(
        message=FormattedText([('class:chat-user-prompt', "Your Query to Refine PR Body: ")]), # This USES the class
        style=chat_session_style_to_use, # This PROVIDES the style object with the class definitions
        bottom_toolbar=get_chat_bottom_toolbar_ft(),
        key_bindings=chat_kb_for_session,
        multiline=True
    )
    


    while True:
        print_styled_chat([('class:chat-highlight', f"\nCurrent Draft being refined in chat:")])
        for line in message_being_refined_in_chat.splitlines(): print_styled_chat([('class:instruction', line)])
        print_styled_chat([('class:chat-separator', "---")])

        user_input_from_prompt = ""
        try:
            with patch_stdout():
                user_input_from_prompt = await chat_session.prompt_async()
        except KeyboardInterrupt: user_input_from_prompt = "/cancel"; print_styled_chat([('class:chat-info', "(Ctrl+C treated as /cancel)")])
        except EOFError: user_input_from_prompt = "/cancel"; print_styled_chat([('class:chat-info', "(Ctrl+D treated as /cancel)")])
        
        if user_input_from_prompt is None: user_input_from_prompt = "/cancel"
        
        cleaned_user_query = user_input_from_prompt.strip()
        is_apply_command = cleaned_user_query.lower() == "/apply" # is_apply_command defined
        
        if not cleaned_user_query and not is_apply_command:
            cleaned_user_query = "/cancel"
            print_styled_chat([('class:chat-info', "(Empty input treated as /cancel)")])

        if cleaned_user_query.lower() != "/apply" or user_input_from_prompt == "/apply":
             print_styled_chat([('class:chat-user-prompt', "You: "), ('class:chat-user-text', cleaned_user_query if cleaned_user_query else "(Action via Keybinding / Empty)")])

        if cleaned_user_query.lower() == "/cancel":
            print_styled_chat([('class:chat-highlight', "\nChat refinement cancelled. Returning PR body from before chat session.")])
            return initial_draft

        elif is_apply_command:
            final_message_to_apply = message_being_refined_in_chat
            if not final_message_to_apply.strip() and initial_draft.strip(): # initial_draft used here
                print_styled_chat([('fg:ansired', "Current draft in chat is empty, but initial was not. Cannot apply empty. Use /cancel or provide content.")])
                continue # continue in loop

            print_styled_chat([('class:chat-highlight', "\nApplying this PR Body from chat:")])
            for line in final_message_to_apply.splitlines(): print_styled_chat([('class:instruction', line)])
            print_styled_chat([('class:chat-separator', "---")])
            
            with patch_stdout():
                confirmed_apply = await pt_confirm_async(
                    "Use this message & exit chat?", # This is message_text
                    style=passed_style # Pass the comprehensive style object
                )

            if confirmed_apply:
                print_styled_chat([('bold fg:ansigreen', "--- PR Body confirmed. Returning to main editor. ---")])
                return final_message_to_apply 
            else:
                print_styled_chat([('class:chat-highlight', "/apply action discarded by user. Continuing chat.")])
            continue # continue in loop
        
        chat_history.append({"role": "user", "content": cleaned_user_query})
        messages_for_llm = [{"role": "system", "content": get_current_chat_system_prompt()}] + chat_history
            
        extracted_proposal_text = None
        llm_full_response_text = ""
        conversational_parts_to_print = []
        conversational_text_for_history = ""

        try:
            print_styled_chat([('class:chat-info', "LLM thinking...")])
            if hasattr(model, "chat") and callable(model.chat):
                response_obj = model.chat(messages_for_llm)
            else:
                prompt_content = _format_chat_history_for_prompt_model(messages_for_llm[1:])
                response_obj = model.prompt(prompt_content, system=get_current_chat_system_prompt())
            
            if hasattr(response_obj, 'text') and callable(response_obj.text):
                 llm_full_response_text = response_obj.text().strip()
            elif isinstance(response_obj, str):
                 llm_full_response_text = response_obj.strip()
            else: 
                 llm_full_response_text = str(response_obj).strip()

            print_styled_chat([('class:chat-llm-prompt', "LLM:")])
            if not llm_full_response_text:
                print_styled_chat([('class:chat-info', "(LLM returned no text)")])
                conversational_text_for_history = ""
            else:
                start_marker_idx = llm_full_response_text.find(PROPOSED_PR_BODY_MARKER_START)
                end_marker_idx = -1
                if start_marker_idx != -1:
                    end_marker_idx = llm_full_response_text.find(PROPOSED_PR_BODY_MARKER_END, start_marker_idx + len(PROPOSED_PR_BODY_MARKER_START))

                if start_marker_idx != -1 and end_marker_idx != -1:
                    conv_before = llm_full_response_text[:start_marker_idx].strip()
                    if conv_before: conversational_parts_to_print.append(conv_before)
                    
                    proposal_start_content_idx = start_marker_idx + len(PROPOSED_PR_BODY_MARKER_START)
                    temp_extracted = llm_full_response_text[proposal_start_content_idx:end_marker_idx].strip()
                    if temp_extracted: extracted_proposal_text = temp_extracted
                    
                    conv_after = llm_full_response_text[end_marker_idx + len(PROPOSED_PR_BODY_MARKER_END):].strip()
                    if conv_after: conversational_parts_to_print.append(conv_after)
                    
                    conversational_text_for_history = "\n".join(filter(None, [conv_before, conv_after])).strip()
                else:
                    conversational_parts_to_print.append(llm_full_response_text)
                    conversational_text_for_history = llm_full_response_text
            
                for part in conversational_parts_to_print:
                    if part:
                        for line in part.splitlines(): print_styled_chat([('class:chat-llm-text', line)])
            
        except Exception as e:
            print_styled_chat([('fg:ansired', f"\nLLM Error: {e}")])
            conversational_text_for_history = f"(LLM Error: {e})"
            extracted_proposal_text = None
            # No specific try/except around this line needed if it's just assignment
            
        assistant_response_for_history_log = "" # Renamed to avoid clash

        if extracted_proposal_text:
            print_styled_chat([('class:chat-separator', "---")])
            print_styled_chat([('class:chat-highlight', "LLM Proposes Update to PR Body Draft:")])
            for line in extracted_proposal_text.splitlines(): print_styled_chat([('class:instruction', line)])
            print_styled_chat([('class:chat-separator', "---")])

            with patch_stdout():
                accepted_proposal = await pt_confirm_async(
                    "Accept this proposal as current chat draft?", # This is message_text
                    style=passed_style # Pass the comprehensive style object
                )

            if accepted_proposal:
                message_being_refined_in_chat = extracted_proposal_text
                print_styled_chat([('bold fg:ansigreen', "Proposal accepted. Current chat draft updated.")])
                assistant_response_for_history_log = extracted_proposal_text
            else:
                print_styled_chat([('class:chat-highlight', "Proposal rejected. Chat draft remains unchanged from before this query.")])
                if conversational_text_for_history: # conversational_text_for_history defined earlier
                    assistant_response_for_history_log = conversational_text_for_history
                else:
                    assistant_response_for_history_log = "(LLM made a proposal which was rejected by the user.)"
        else:
             assistant_response_for_history_log = conversational_text_for_history

        if assistant_response_for_history_log or not llm_full_response_text:
            chat_history.append({"role": "assistant", "content": assistant_response_for_history_log})
            
        print_styled_chat([('class:chat-separator', "---")])


async def pt_confirm_async(message_text="Confirm?", default=True, style=None): # message_text is now just a string
    """Async wrapper for a confirm prompt using PromptSession."""
    # Use a specific class from the style if you want to style the confirm message,
    # or rely on prompt_toolkit's default styling for confirms.
    # Let's assume you want to use 'chat-highlight' for the prompt message.
    formatted_message = FormattedText([('class:chat-highlight', message_text + " (Y/n): ")])
    
    session = PromptSession(message=formatted_message, style=style) # Pass the comprehensive style
    while True:
        try:
            with patch_stdout():
                result = await session.prompt_async(default='y' if default else 'n')
            if result is None: return default
            result_lower = result.lower().strip()
            if result_lower == 'y': return True
            if result_lower == 'n': return False
            # Optional: print "Please answer y or n"
        except (KeyboardInterrupt, EOFError):
            return default

def _format_chat_history_for_prompt_model(chat_history: list) -> str:
    if not chat_history: return "No conversation history yet."
    return "\n".join([f"{msg['role'].capitalize()}: {msg['content']}" for msg in chat_history])


def _create_github_pr_gh(repo_slug, target_branch, source_branch, title, body, draft):
    body_file_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".md", encoding="utf-8") as tmp_body_file:
            tmp_body_file.write(body)
            body_file_path = tmp_body_file.name

        command = [
            "gh", "pr", "create",
            "--repo", repo_slug,
            "--base", target_branch,
            "--head", source_branch,
            "--title", title,
            "--body-file", body_file_path
        ]
        if draft: command.append("--draft")

        click.echo(f"\nRunning: {' '.join(command)}")
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="ignore", check=False)

        if process.returncode == 0:
            click.echo(click.style(f"\nSuccessfully {'Draft ' if draft else ''}Pull Request created/viewed!", fg="green"))
            if process.stdout: click.echo(process.stdout)
            if process.stderr: click.echo(click.style(f"gh stderr (non-fatal):\n{process.stderr}", fg="yellow"))
        else:
            click.echo(click.style(f"Error creating PR using 'gh' (exit code: {process.returncode}):", fg="red"), err=True)
            if process.stdout: click.echo(f"gh stdout:\n{process.stdout}", err=True)
            if process.stderr: click.echo(f"gh stderr:\n{process.stderr}", err=True)
            click.echo(click.style("Please ensure 'gh' is authenticated (`gh auth status`), the branches exist on the remote, and parameters are correct.", fg="yellow"), err=True)
            
    except FileNotFoundError: # This try/except is for NamedTemporaryFile or subprocess.run for 'gh'
        click.echo(click.style("Error: 'gh' command not found. Is GitHub CLI installed and in your PATH?", fg="red"), err=True)
    except Exception as e:
        click.echo(click.style(f"An unexpected error occurred while trying to use 'gh' or manage temp file: {e}", fg="red"), err=True)
    finally:
        if body_file_path and os.path.exists(body_file_path):
            os.remove(body_file_path)