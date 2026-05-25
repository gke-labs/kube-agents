import asyncio
import os
from dotenv import load_dotenv
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.utils.interactive import run_interactive_loop
from logger import get_logger

logger = get_logger("AGY-SDK agent")

# Load environment variables from .env file in the current directory
load_dotenv()


async def main():
  # Retrieve the Gemini API key from the environment variables (.env)
  api_key = os.environ.get("GEMINI_API_KEY")

  if not api_key:
    logger.error(
        "Gemini API Key is not set. Please configure GEMINI_API_KEY in your"
        " .env file."
    )
    return

  # Configure the agent to use gemini-3.5-flash model and the API key
  config = LocalAgentConfig(
      model="gemini-3.5-flash",
      api_key=api_key,
  )

  logger.info("Initializing Antigravity Agent with model gemini-3.5-flash...")
  async with Agent(config) as agent:
    await run_interactive_loop(agent)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except KeyboardInterrupt:
    logger.info("Goodbye!")
