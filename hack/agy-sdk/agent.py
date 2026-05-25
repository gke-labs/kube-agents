import argparse
import asyncio
from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.utils.interactive import run_interactive_loop
from logger import get_logger

logger = get_logger('AGY-SDK agent')


def parse_args():
  parser = argparse.ArgumentParser(description="Run Antigravity Agent locally.")
  parser.add_argument(
      "--api-key",
      type=str,
      required=True,
      help="Gemini API Key (required).",
  )
  return parser.parse_args()


async def main():
  args = parse_args()
  api_key = args.api_key

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
