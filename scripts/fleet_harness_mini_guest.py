"""Pinned Mini control adapter, executed in its own networkless container.

No shell execution or HTTP here. CONTROL supplies one retained response and
the exact observations of its separately confined executor. Mini's actual
DefaultAgent query/actions/serialization and native submit parser are reused.
"""
import copy
import json
import os
import sys
from types import SimpleNamespace

os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = "/tmp/mini-private"
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT"] = "1"
sys.path.insert(0, "/deps")

from minisweagent import __version__
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import InterruptAgentFlow
from minisweagent.models.utils.actions_toolcall import parse_toolcall_actions, format_toolcall_observation_messages

SYSTEM = "You are the sole Worker. Use bash within the supplied scope. Return the exact owner JSON as the content accompanying the sole echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT tool call."
SENTINEL = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


class RetainedModel:
    response = None

    def query(self, messages):
        response = copy.deepcopy(self.response)
        choices = response["choices"]
        if len(choices) != 1 or choices[0]["message"].get("role") != "assistant":
            raise ValueError("ambiguous provider response")
        message = copy.deepcopy(choices[0]["message"])
        calls = message.get("tool_calls", [])
        native = [SimpleNamespace(id=call["id"], function=SimpleNamespace(**call["function"])) for call in calls]
        actions = parse_toolcall_actions(native, format_error_template="{{ error }}")
        ids = [action["tool_call_id"] for action in actions]
        if (len(set(ids)) != len(ids) or any(not isinstance(action["command"], str) for action in actions)
                or any(action["command"] == SENTINEL for action in actions) and len(actions) != 1):
            raise ValueError("invalid action batch")
        message["extra"] = {"response": response, "actions": actions, "cost": 0.0,
                            "cost_semantics": "native accumulator disabled; authoritative budget is CONTROL"}
        return message

    def format_message(self, **kwargs): return kwargs

    def get_template_vars(self): return {"model_name": "deepseek/deepseek-flash"}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return format_toolcall_observation_messages(actions=message["extra"]["actions"], outputs=outputs,
            observation_template="<returncode>{{output.returncode}}</returncode>\n<output>{{output.output}}</output>", template_vars={})

    def serialize(self):
        return {"info": {"config": {"model": {"model_name": "deepseek/deepseek-flash", "transport": "control-retained-response-v1"}}}}


class RetainedEnvironment:
    outputs = []

    def execute(self, action):
        if not self.outputs:
            raise ValueError("missing CONTROL tool observation")
        output = self.outputs.pop(0)
        if output.pop("tool_call_id") != action["tool_call_id"]:
            raise ValueError("tool observation belongs to another action")
        # Reuse only the native pure terminal recognizer. No LocalEnvironment
        # instance, execute(), template vars or inherited host environment.
        LocalEnvironment._check_finished(None, output)
        return output

    def get_template_vars(self): return {"cwd": "/candidate"}

    def serialize(self): return {"info": {"config": {"environment": {"cwd": "/candidate", "execution": "external-owned-sandbox"}}}}


def main():
    agent = None
    model, environment = RetainedModel(), RetainedEnvironment()
    for line in sys.stdin.buffer:
        if len(line) > 2 * 1024 * 1024: return
        request = json.loads(line)
        value, error = None, None
        try:
            if __version__ != "2.4.6": raise ValueError("unreviewed Mini version")
            if request["op"] == "init":
                if agent is not None: raise ValueError("cannot replace an admitted Mini task")
                agent = DefaultAgent(model, environment, system_template=SYSTEM, instance_template="{{task}}",
                    cost_limit=0, step_limit=32, output_path=None)
                agent.extra_template_vars = {"task": request["task"]}
                agent.add_messages(model.format_message(role="system", content=agent._render_template(SYSTEM)),
                    model.format_message(role="user", content=agent._render_template("{{task}}")))
            elif request["op"] == "query":
                if agent is None or agent.messages[-1]["role"] in {"assistant", "exit"}:
                    raise ValueError("query at invalid native frontier")
                model.response = request["response"]
                agent.query()
            elif request["op"] == "restore":
                retained=request["trajectory"]
                if (agent is None or len(agent.messages)!=2 or retained["info"]["mini_version"]!="2.4.6"
                        or retained["messages"][:2] != agent.messages
                        or type(retained["info"]["model_stats"]["api_calls"]) is not int
                        or not 0<=retained["info"]["model_stats"]["api_calls"]<=32):
                    raise ValueError("foreign or invalid pure Mini restore")
                agent.messages=copy.deepcopy(retained["messages"])
                agent.n_calls=retained["info"]["model_stats"]["api_calls"]
                agent.cost=retained["info"]["model_stats"]["instance_cost"]
            elif request["op"] == "observe":
                if agent is None or agent.messages[-1]["role"] != "assistant":
                    raise ValueError("observation at invalid native frontier")
                environment.outputs = copy.deepcopy(request["outputs"])
                if len(environment.outputs) != len(agent.messages[-1]["extra"]["actions"]):
                    raise ValueError("observation batch differs")
                try:
                    agent.execute_actions(agent.messages[-1])
                except InterruptAgentFlow as exc:
                    agent.add_messages(*exc.messages)
                if environment.outputs: raise ValueError("unused tool observations")
            else:
                raise ValueError("unsupported control operation")
            value = agent.serialize()
        except BaseException as exc:
            error = type(exc).__name__ + ":" + str(exc)[:500]
        sys.stdout.write(json.dumps({"id": request["id"], "value": value, "error": error, "input_after": None}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__": main()
