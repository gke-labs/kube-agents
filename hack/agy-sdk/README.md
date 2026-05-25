# Antigravity SDK based agent

This directory contains the implementation and instructions for running a basic local AI agent using the [Google Antigravity SDK](https://github.com/google-antigravity/antigravity-sdk-python).

The agent is configured to use the `gemini-3.5-flash` model and runs an interactive command-line chat loop.

## Project Directory Structure

```
hack/agy-sdk/
├── .env                 # (Git-ignored) Your local variables and secrets
├── .env.example         # Safe template showing needed variables      
├── README.md            # This documentation file
│
└── app/                 # 📦 Agent code
    ├── requirements.txt # Dependencies file
    └── agent.py         # Main agent script
```

## Prerequisites

Ensure you have Python 3.10 or higher installed on your system. The `google-antigravity` package strictly requires `Python >= 3.10` to install and run.

## 1. Navigate to the Agent Directory

Before running any commands, navigate to this directory from the root of your repository:

```bash
cd hack/agy-sdk
```

## 2. Install Dependencies

Create a Python virtual environment in this directory to manage dependencies cleanly, and install the requirements from `app/requirements.txt`:

```bash
# Create a virtual environment
python3 -m venv venv

# Activate the virtual environment
source venv/bin/activate

# Install the dependencies from app/requirements.txt
pip install -r app/requirements.txt
```

## 3. Configure Environment Variables

The agent loads configuration variables from a local `.env` file in this directory.

1. **Create the `.env` file** by copying the template:
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env`** and configure your Gemini API Key:
   ```text
   GEMINI_API_KEY="your-actual-api-key-here"
   ```

## 4. Run the Agent Local Chat

Once you have configured your `.env` file, you can run the agent locally by simply executing:

```bash
python3 app/agent.py
```

### Interactive CLI Commands
- Enter any message or question in the `User: ` prompt to chat with the agent.
- Type `exit` or `quit` to end the session.
- Press `Ctrl+C` to exit the loop cleanly at any time.
