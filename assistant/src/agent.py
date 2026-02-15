import logging
from typing import Optional

from langchain.chat_models import init_chat_model
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from prompts import SYSTEM_PROMPT, USER_PROMPT

logger = logging.getLogger(__name__)


class ITopInfoChecker:
    def __init__(self, model_name: str):
        """
        Initialize the AI checker with a specified LLM.

        :param model_name: Full model name (e.g., 'google_genai:gemini-1.5-flash').
        """
        self.model_name = model_name

        self.llm = self._init_llm()
        self._setup_chain()

    def _init_llm(self):
        """
        Initialize LLM using langchain's init_chat_model for vendor-agnostic support.
        """
        try:
            return init_chat_model(model=self.model_name)
        except Exception as e:
            logger.error(f"Failed to initialize LLM for model {self.model_name}: {e}")
            raise

    def _setup_chain(self):
        prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("user", USER_PROMPT)])

        self.chain = prompt | self.llm | StrOutputParser()

    async def check_completeness(
        self, title: str, description: str, service_desc: str, subcategory_desc: str
    ) -> Optional[str]:
        """
        Check if the ticket description has enough information.
        Returns the missing info message or None if everything is OK.
        Raises an exception if the LLM check fails.
        """
        logger.debug("Running completeness check via LLM")
        response = await self.chain.ainvoke(
            {
                "title": title,
                "description": description,
                "service_description": service_desc,
                "subcategory_description": subcategory_desc,
            }
        )

        response = response.strip()
        if response.upper() == "OK":
            return None
        return response
