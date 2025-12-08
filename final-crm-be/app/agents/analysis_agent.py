from dis import Instruction
import logging

from numpy import number
from app.agents.base_agent import BaseAgent
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from app.agents.tools.makedocs_tools import  convert_to_pdf, create_docx, create_pptx


logger = logging.getLogger(__name__)

class AnalysisAgent(BaseAgent):
    """
    Agent dedicated to creating .docx files from provided text and saving locally.
    
    Workflow:
    1. Receives text via the 'query' parameter.
    2. Calls the 'make_docx_file' tool to generate a document.
    """

    def get_agent_name(self) -> str:
        """Get unique agent name"""
        return "analysis_agent"

    def create_agent(self) -> LlmAgent:
        """
        Create and configure the Docx Creation agent.
        """
        struktur_docx = {
            "spec": {
                "title": "",
                "subtitle": "",
                "metadata": {
                "author": "",
                "category": ""
                },
                "sections": [
                {
                    "heading": "",
                    "paragraphs": ["",""]
                },
                {
                    "heading": "",
                    "bullets": ["", ""]
                },
                {
                    "heading": "",
                    "paragraphs": [""],
                    "table": {
                    "headers": ["", "", "", "", ""],
                    "rows": [
                        ["[]", "[]", "[]", "[]", "[]"]
                    ]
                    }
                },
                {
                    "heading": "",
                    "bullets": ["", ""]
                },
                {
                    "heading": "",
                    "bullets": ["", ""]
                },
                {
                    "heading": "",
                    "paragraphs": ["", ""]
                }
                ]
            },
            }

        struktur_pptx = {
            "title": "",
            "subtitle": "",
            "theme": "",
            "purpose": "",
            "audience": "",
            "style_preset": "",
            "slides": [
                {
                "title": "",
                "content": [
                    "",
                    "",
                ]
                },
                {
                "title": "",
                "content": [
                    "",
                    "",
                ],
                "layout": ""
                },
            ],
            "num_slides": number
            }

        instruction = (
            "Role: You are a professional AI assistant specialized in business administration document creation.\n\n"
            "Goal: Your core objective is to generate high-quality, professional business documents in DOCX, PDF, or PPTX formats.\n\n"
            "Functional Scope: You are strictly limited to document-related operations — creating, editing, formatting, and exporting documents in DOCX, PDF, or PPTX. Reject any unrelated tasks such as web scraping, data analysis, or system automation.\n\n"
            "Output Format Rules: Always produce a final document file (.docx, .pdf, or .pptx). If the user does not specify a format, use DOCX by default.\n\n"
            "Interaction Policy: If any instruction is ambiguous, pause and ask a short clarification with limited options (e.g., choose between template A/B, document length, or audience type) before continuing.\n\n"
            "Available Tools:\n"
            f"1. create_docx(spec={struktur_docx}, email='email', filename='filename.docx') — create a Word document\n"
            "2. convert_to_pdf(filename='filename', email='email') — convert a DOCX file into PDF and return a download URL\n"
            f"3. create_pptx(spec={struktur_pptx}, email='email', filename='filename.pptx') — create a PowerPoint presentation and return a download URL\n\n"
            "Workflow:\n"
            "1. Analyze the content and user intent\n"
            "2. Build the document data structure (spec)\n"
            "3. Create the document\n"
            "4. Perform a quality and format check\n"
            "5. Export and deliver the final file\n"
            "Always follow this sequence and use an internal checklist to verify completeness and compliance at each step. Escalate to clarification if information is missing or unclear.\n\n"
            "Memory Policy: Retain only persistent data relevant to document quality — such as project goals, audience, writing style, preferred templates, glossary terms, output format preferences, and pending placeholders for user input.\n\n"
            "Response Format:\n"
            "Provide a confirmation message and a download URL for the generated file."
        )

        agent = LlmAgent(
            name=self.get_agent_name(),
            model=LiteLlm(model="openai/gpt-3.5-turbo"),
            instruction=instruction,
            output_key="text",
            tools=[
                create_docx, # tools membuat dokumen docx
                convert_to_pdf, # tools mengonversi dokumen docx menjadi pdf
                create_pptx, # tools membuat presentasi pptx
            ]
        )

        print("AGENT",agent)
        logger.info(f"Created {self.get_agent_name()} with 2 tools: build_context_from_query and render_docx_generic")
        return agent

    async def run(
        self, user_id: str,email:str, query: str,session_state: dict = None,
        session_id: str = None
    ) -> dict:
        """
        Run the Docx Creation agent (local save).
        """
        print("SESSION STATE",session_state)
        print("SESSION ID RUN", session_id)
        # Prepare session state
        state = session_state or {}
        state["user:email"] = user_id
        state["temp:last_query"] = query

        tool_output, final_session_id = await super().run(
            user_id=user_id,
            email=email,
            query=query,
            session_state=state,
            session_id=session_id
        )

        print("TOOLS OUTPUT",tool_output)
        # tool_output diasumsikan berisi URL dari upload_to_supabase
        # Normalisasi agar API mengembalikan download_url
        # download_url = tool_output.get("public_url") or tool_output.get("signed_url") or tool_output
        return {"message": tool_output, "session_id": final_session_id}
