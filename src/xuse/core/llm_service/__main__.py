import asyncio
import logging

from xuse.core.config_loader import ConfigLoader
from .service import LLMService


async def main_test():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    loader = ConfigLoader()
    if not loader.get_settings():
        logging.error("settings.json not found or empty. LLMService test cannot proceed effectively.")
        return
    llm = LLMService(config_loader=loader)
    prompt = "Write a short, engaging tweet about the future of AI in three sentences."
    text = await llm.generate_text(prompt)
    if text:
        logging.info(f"Response: {text}")
    else:
        logging.warning("No response or client not configured.")


if __name__ == '__main__':
    asyncio.run(main_test())

