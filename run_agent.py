"""
启动设备运维 Agent
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.agent_engine import DeviceAgent, interactive_shell

def main():
    agent = DeviceAgent(use_llm=True)
    agent.start()
    try:
        interactive_shell(agent)
    finally:
        agent.stop()

if __name__ == "__main__":
    main()
