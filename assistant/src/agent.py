import logging
from typing import Optional

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from prompts import SYSTEM_PROMPT, USER_PROMPT

logger = logging.getLogger(__name__)


class ITopInfoChecker:
    def __init__(self, model_name: str, base_url: str, api_key: SecretStr | None = None):
        """
        Initialize the AI checker with an LM Studio (OpenAI-compatible) model.

        :param model_name: Model name as shown in LM Studio (e.g., 'qwen2.5-7b-instruct').
        :param base_url: LM Studio API base URL (e.g., 'http://localhost:1234/v1').
        :param api_key: API key — LM Studio does not validate it, any string works.
        """
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key

        self.llm = self._init_llm()
        self._setup_chain()

    def _init_llm(self):
        try:
            return ChatOpenAI(
                model=self.model_name,
                base_url=self.base_url,
                api_key=self.api_key,
            )
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
