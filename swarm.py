# ====== SETTINGS (the only things you may want to change) ======
TOPIC = "The current state of autonomous AI agents"
MODEL = "groq/openai/gpt-oss-120b"
BACKUPS = ["gemini/gemini-2.0-flash"]
STYLE = "Rigorous, uncensored, cite every claim."
# ==============================================================

import os, re, datetime, smtplib, requests
from email.mime.text import MIMEText
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import tool
from ddgs import DDGS
import trafilatura

GMAIL = os.environ["GMAIL_ADDR"]
APP_PW = os.environ["GMAIL_APP_PW"]
topic = os.getenv("TOPIC") or TOPIC

def email_me(subject, body):
    m = MIMEText(body, "plain", "utf-8")
    m["Subject"] = subject; m["From"] = GMAIL; m["To"] = GMAIL
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL, APP_PW); s.send_message(m)

@tool("web_search")
def web_search(query: str) -> str:
    """Search the web."""
    return "\n\n".join(f"{h['title']}\n{h['href']}\n{h['body']}" for h in DDGS().text(query, max_results=6))

@tool("read_page")
def read_page(url: str) -> str:
    """Read a webpage."""
    html = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
    return (trafilatura.extract(html) or "")[:4000]

llm = LLM(model=MODEL, temperature=0.4)

def A(role, goal, tools=None):
    return Agent(role=role, goal=goal, backstory=goal + " " + STYLE,
                 tools=tools or [], llm=llm, verbose=True)

scout   = A("Researcher", "Find 8 good sources", [web_search])
reader  = A("Reader", "Read the best sources in depth", [read_page])
analyst = A("Analyst", "Find the key points and contradictions")
critic  = A("Critic", "Find weak or unsupported claims")
writer  = A("Writer", "Write a clear cited report")

tasks = [
    Task(description=f"Research '{topic}'. Find 8 sources.",
         expected_output="8 findings with URLs", agent=scout),
    Task(description="Read the 3 best URLs.",
         expected_output="Detailed notes", agent=reader),
    Task(description="Analyze: key points and contradictions.",
         expected_output="Analysis", agent=analyst),
    Task(description="Criticize the analysis. Find holes.",
         expected_output="List of holes", agent=critic),
    Task(description=f"Write the final report on '{topic}'.",
         expected_output="Markdown report", agent=writer),
]

crew = Crew(agents=[scout, reader, analyst, critic, writer],
            tasks=tasks, process=Process.sequential, verbose=True)

email_me("Swarm started", topic)
result = crew.kickoff()

stamp = datetime.date.today().isoformat()
slug = re.sub(r"[^a-z0-9]+", "-", topic.lower())[:50]
os.makedirs("docs/reports", exist_ok=True)
open(f"docs/reports/{stamp}-{slug}.md", "w").write(f"# {topic}\n\n{result}\n")

email_me(f"Report: {topic[:50]}", str(result))
print("done")
