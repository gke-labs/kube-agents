import os
import json
import logging
import re
from typing import Dict, Any, Optional

logger = logging.getLogger("hermes.plugin.compliance_critic")

def register(ctx):
    llm = ctx.llm

    def run_critic_and_schedule(
        response_text: str,
        session_id: str,
        model: str,
        platform: str,
        **kwargs,
    ) -> Optional[str]:
        """
        Programmatic Critic: Analyzes the final response to check if an async wait
        is required and if a cronjob was scheduled. If missing, it schedules it.
        """
        try:
            import pathlib
            prompt_path = pathlib.Path(__file__).parent / "TASK_SUCCESS_CRITERIA.md"
            if prompt_path.exists():
                critic_prompt = prompt_path.read_text(encoding="utf-8")
            else:
                logger.warning("TASK_SUCCESS_CRITERIA.md not found, using default fallback prompt")
                critic_prompt = "Verify if response is compliant with turn completion constraint."

            schema = {
                "type": "object",
                "properties": {
                    "is_async_or_pending": {"type": "boolean"},
                    "is_compliant": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "recommended_followup_prompt": {"type": "string"},
                    "recommended_schedule": {"type": "string"}
                },
                "required": ["is_async_or_pending", "is_compliant", "reason"]
            }

            logger.info("Compliance Critic running structured evaluation...")
            result = llm.complete_structured(
                instructions=critic_prompt,
                input=[{"type": "text", "text": f"Proposed Response:\n{response_text}"}],
                json_schema=schema
            )

            parsed_result = result.parsed
            if not isinstance(parsed_result, dict):
                logger.error(f"Critic failed to return a valid JSON object. Parsed: {parsed_result}")
                return response

            is_async_waiting = parsed_result.get("is_async_or_pending", False)
            is_compliant = parsed_result.get("is_compliant", True)
            reason = parsed_result.get("reason", "")

            logger.info(f"Critic Evaluation: async_waiting={is_async_waiting}, compliant={is_compliant}, reason='{reason}'")

            # 3. Schedule Cronjob if missing
            if is_async_waiting and not is_compliant:
                followup_prompt = parsed_result.get("recommended_followup_prompt")
                schedule = parsed_result.get("recommended_schedule", "60s")

                if not followup_prompt:
                    followup_prompt = f"Check status of pending operation in session {session_id} and continue execution."

                logger.warning(f"Critic detected missing cronjob for pending async state! Scheduling programmatically...")

                # Import cron engine dynamically
                from cron.jobs import create_job

                # Extract provider/model from current execution model
                prov, model_name = None, None
                if model and "/" in model:
                    prov, model_name = model.split("/", 1)
                else:
                    model_name = model

                # Schedule the job to run once (repeat=1) to wake the agent up
                job = create_job(
                    prompt=followup_prompt,
                    schedule=schedule,
                    name=f"auto-followup-{session_id[:8]}",
                    repeat=1,
                    provider=prov,
                    model=model_name,
                )

                job_id = job["id"]
                logger.info(f"Successfully scheduled programmatic follow-up job: {job_id}")

                # 4. Inject notification into final response so the user and future agents are aware
                compliance_footer = (
                    f"\n\n---\n"
                    f"⚠️ **Swarm Compliance Guard:** Detected pending asynchronous operations. "
                    f"Programmatically scheduled follow-up check in **{schedule}** via Cronjob "
                    f"(`Job ID: {job_id}`).\n"
                    f"*Follow-up Prompt:* \"{followup_prompt}\""
                )
                return response_text + compliance_footer

        except Exception as e:
            logger.error(f"Compliance Critic error in transform_llm_output hook: {e}", exc_info=True)

        return None  # Return None to pass through original response unchanged

    ctx.register_hook("transform_llm_output", run_critic_and_schedule)
