from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from app.agents.base_agent import BaseAgent, logger
from app.agents.tools.docx_tools import create_document


class DataAnalystAgent(BaseAgent):
	"""
	Data Analyst Agent

	An agent specialized in analyzing datasets, generating insights, and creating visualizations.
	Utilizes tools for data processing and visualization to assist users in understanding their data.

	Capabilities:
	- Analyze datasets to extract key insights
	- Generate visualizations (charts, graphs) to represent data trends
	- Answer questions related to data analysis and interpretation
	- Provide recommendations based on data patterns

	Tools:
	- Data Processing Tool: For cleaning and transforming datasets
	- Visualization Tool: For creating charts and graphs

	Example Use Cases:
	- "Analyze this sales dataset and provide insights on trends."
	- "Create a bar chart showing monthly revenue."
	- "What are the key factors affecting customer churn in this dataset?"
	"""

	def create_agent(self) -> BaseAgent:
		# Implementation for creating and configuring the Data Analyst Agent
		"""
        Create and configure the RAG agent.

        Returns:
            LlmAgent configured for RAG tasks
        """
		instruction = (
			"You are an AI Customer Support Assistant using Retrieval-Augmented Generation (RAG). "
			"You MUST only answer questions based on documents embedded in ChromaDB. "
			"\n\n"
			"Follow this process strictly:\n"
			"1. Detect the language of the user's input. "
			"2. Retrieve relevant context using `get_context_documents_ChromaDB`. "
			"3. Re-rank results using `rerank_with_openai_after_get_context`. "
			"4. Generate a clear and concise answer ONLY from the most relevant documents, "
			"in the same language as the user's question. "
			"5. If no sufficient information is found in the documents, answer with: "
			"- In English: 'Sorry, I cannot answer this yet due to insufficient data.' "
			"- In Indonesian: 'Maaf, saya masih belum bisa menjawab karena kekurangan data.' "
			"\n\n"
			"Important rules:\n"
			"- Do NOT use outside knowledge. "
			"- Do NOT make assumptions or invented answers. "
			"- Always respond in the same language as the user input. "
			"- Keep answers short, clear, and professional. "
			"- Final output to the user must contain ONLY the answer text (no extra system notes)."
		)

		agent = LlmAgent(
			name=self.get_agent_name(),
			model=LiteLlm(model="openai/gpt-3.5-turbo"),
			instruction=instruction,
			output_key="final_text",
			tools=[
				create_document
			]
		)

		logger.info(f"Created {self.get_agent_name()} with 2 tools")

		return agent

	def get_agent_name(self) -> str:
		return "data_analyst_agent"
