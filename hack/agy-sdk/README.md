# Antigravity SDK based agent

This directory contains the implementation and instructions for running a basic local AI agent using the [Google Antigravity SDK](https://github.com/google-antigravity/antigravity-sdk-python).

The agent is configured to use the `gemini-3.5-flash` model and runs an interactive command-line chat loop.

## Prerequisites

Ensure you have Python 3.10 or higher installed on your system. The `google-antigravity` package strictly requires `Python >= 3.10` to install and run.

## 1. Navigate to the Agent Directory

Before running any commands, navigate to this directory from the root of your repository:

```bash
cd hack/agy-sdk
```

## 2. Install Dependencies

Create a Python virtual environment in this directory to manage dependencies cleanly, and install the requirements from `requirements.txt`:

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install the dependencies from requirements.txt
pip install -r requirements.txt
```

## 3. Run the Agent Local Chat

To start the interactive chat session, export your Gemini API key as an environment variable and then run the agent script passing the variable to the `--api-key` argument:

```bash
# Export the Gemini API key
export GEMINI_API_KEY="your-actual-api-key-here"

# Start the agent using the exported variable
python3 agent.py --api-key $GEMINI_API_KEY
```

If the `--api-key` argument is not provided, the program will print an error and exit.

### Interactive CLI Commands
- Enter any message or question in the `User: ` prompt to chat with the agent.
- Type `exit` or `quit` to end the session.
- Press `Ctrl+C` to exit the loop cleanly at any time.
