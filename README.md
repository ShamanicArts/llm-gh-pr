# llm-gh-pr

llm plugin to generate GitHub Pull Request titles and bodies based on repository changes and interactively create PRs.

## Installation

This plugin requires [llm](https://llm.datasette.io/) and the [GitHub CLI (`gh`)](https://cli.github.com/) to be installed.

You can install this plugin using `llm install` like so:

```bash
llm install llm-gh-pr
```

Ensure you have the GitHub CLI (`gh`) installed and authenticated:
```bash
gh auth login
```

## Usage

From within a Git repository, run:

```bash
llm gh-pr
```

This command will:
1.  Identify new commits on your current branch compared to the target branch (defaulting to `main` or auto-detected).
2.  Optionally include the `git diff` as context.
3.  Send this context to your configured Large Language Model to generate a PR title and body.
4.  Present the LLM-generated PR title and body for you to review and edit.

**Interactive PR Message Editing and Refinement:**
The plugin provides a powerful interactive interface for reviewing, editing, and refining the LLM-generated PR title and body.

-   **Initial Editing:** You can directly edit the suggested title and body.
    -   For the PR Title: Type and press `Enter` to submit. `Ctrl+C` or `Ctrl+D` to cancel.
    -   For the PR Body:
        -   To add a NEW LINE: Press `Enter`.
        -   To SUBMIT message: Press `Esc`, then `Enter` (or `Alt+Enter`/`Option+Enter`).
        -   To CANCEL: Press `Ctrl+C` or `Ctrl+D`.

-   **Interactive Chat Refinement (Ctrl+I):**
    *   During the PR body editing phase, press `Ctrl+I` to enter a dedicated chat mode.
    *   In this mode, you can converse with the LLM to iteratively refine the PR body.
    *   **How it works:**
        1.  Type your queries, feedback, or additional instructions to the LLM.
        2.  The LLM will respond conversationally and may propose a new version of the PR body.
        3.  If the LLM proposes a new message, you will be prompted to accept (Y) or reject (N) it. Accepting updates the current draft.
        4.  The chat continues until you decide to finalize the message.
    *   **Chat Mode Commands:**
        -   `/apply` or `Ctrl+A`: Use the current draft of the PR body and exit chat mode, returning to the main editor.
        -   `/cancel`: Discard any changes made in the chat session and exit, returning the message as it was when you entered chat mode.

After submitting the message (or if using `-y`), you'll get a final confirmation before the `gh pr create` command is executed.

### Options

-   `-t`, `--target-branch`: The branch to merge into (e.g., `main`, `develop`). Attempts to auto-detect if not provided.
-   `-s`, `--source-branch`: The branch to merge from (your feature branch). Defaults to current branch.
-   `--title`: Provide PR title directly, skipping LLM generation for it.
-   `--body`: Provide PR body directly, skipping LLM generation for it.
-   `-m MODEL_ID`, `--model MODEL_ID`: Specify which LLM model to use.
-   `-S SYSTEM_PROMPT`, `--system SYSTEM_PROMPT`: Custom system prompt for PR generation.
-   `--max-commits-chars`: Max characters from commit logs to send to LLM (default: 10000).
-   `--max-diff-chars`: Max characters from git diff to send to LLM (default: 15000).
-   `--include-diff`: Include git diff along with commit messages as context for the LLM.
-   `--draft`: Create the PR as a draft.
-   `-y`, `--yes`: Skip interactive editing and use the LLM's suggestions directly (still asks for final confirmation).

## The System Prompt

The plugin uses a specific system prompt to guide the LLM in generating PR titles and bodies. Here's the default:

```
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
```

## Configuration

You can configure `llm-gh-pr` using `llm`'s configuration system. This allows you to set default values for options like the model, system prompt, and character limits.

To open your `llm` configuration file, run:

```bash
llm config path
```

Then, edit the `config.json` file (or `config.yaml` if you prefer YAML) to add an `llm-gh-pr` section. For example:

```json
{
    "plugins": {
        "llm-gh-pr": {
            "model": "gpt-4",
            "system_pr_generation": "You are a helpful assistant...",
            "default_target_branch": "main",
            "max-commits-chars": 12000,
            "max-diff-chars": 18000
        }
    }
}
```

## Development

To set up this plugin locally for further development:

1.  Ensure you have the project code in a local directory.
2.  It's recommended to use a Python virtual environment:
    ```bash
    cd path/to/your/llm-gh-pr
    python3 -m venv venv
    source venv/bin/activate  # On Linux/macOS
    # .\venv\Scripts\activate  # On Windows
    ```
3.  Install the plugin in editable mode along with its dependencies (including `llm` itself if not in the venv):
    ```bash
    pip install -e .
    ```
    Now you can modify the code, and the changes will be live when you run `llm gh-pr`.

---