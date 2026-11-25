from __future__ import annotations

TAILOR_SYSTEM_PROMPT = (
    "You are a CV tailoring assistant helping a candidate adapt their CV for a "
    "specific job application.\n\n"
    "Rules — you MUST follow every rule:\n"
    "1. NEVER invent experience, skills, technologies, or any other facts. Only "
    "rephrase and emphasize existing content from the candidate's evidence bank.\n"
    "2. Only modify the Summary section, project bullet points, and Skills. All other "
    "sections (Education, Work Experience, header) must remain exactly "
    "as they are in the template.\n"
    "3. The adapted CV must still be valid Typst markup.\n"
    "4. Keep the summary concise — 2-4 sentences, at most 60 words.\n"
    "5. For each project, you may reorder bullets, rephrase them, or remove/"
    "replace bullets to emphasize job-relevant aspects — but never add facts "
    "not present in the evidence.\n"
    "6. Return ONLY valid JSON with no extra commentary.\n"
    "7. Try to keep the adapted CV about the same length as the original once,"
    "since they are one page long and that is the goal."
)


def build_tailor_user_prompt(
    evidence_yaml: str, job_description: str, template_text: str
) -> str:
    header = "## Candidate Evidence (locked, do not add to this)\n\n"
    body = "## Job Description\n\n"
    body += f"{job_description}\n\n"
    body += "## Current CV Template (editable: Summary, project bullets)\n\n"
    body += f"{template_text}\n\n"
    body += "## Instructions\n\n"
    body += "Return a JSON object with these fields:\n"
    body += (
        '- "summary": rewritten Summary for this job'
        " (2-4 sentences, max 60 words). Use only evidence facts.\n"
    )
    body += (
        '- "projects": dict keyed by project name (exactly as in template).'
        " Each value has:\n"
    )
    body += (
        '  - "bullets": bullet strings (start with "- ").'
        " Emphasize job-relevant aspects. Use only evidence facts. Can be empty.\n"
    )
    body += "\nExample:\n"
    body += '{"summary": "Backend-focused...",\n'
    body += (
        ' "projects": {"Automatic PDF Sorting":'
        ' {"bullets": ["- Built Dockerized backend..."]},\n'
    )
    body += '  "Rocket League": {"bullets": []}}}\n'
    return header + evidence_yaml + "\n\n" + body
