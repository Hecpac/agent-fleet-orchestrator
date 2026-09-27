# Preserved pure consumer reviewed in evolution/20260920-01; original SHA256 8918dee0b7dea44da97482e2c99de30ca0a414d8c70429a8b7bae86d975d77ff.
"""Recover mini's existing terminal content, preserving the original submission.

Pure observation, no model invocation, candidate execution or semantic verdict.
The caller binds and captures the stopped trial's native artifact bytes.
"""
import hashlib
import json
import re


class InvalidTerminal(ValueError):
    pass


TEMPLATE_SHA256 = '546a89156d7823eb34eb49c5b31a3703df4d27639d034a6d13f0162488d70821'


def recover_mini(trajectory, runner, *, task, expected_template_sha256=TEMPLATE_SHA256):
    if (trajectory.get('trajectory_format') != 'mini-swe-agent-1.1'
            or trajectory.get('info', {}).get('mini_version') != '2.4.6'):
        raise InvalidTerminal('unsupported native format/version')
    info = trajectory['info']
    if (info.get('exit_status') != 'Submitted' or runner.get('finish_reason') != 'Submitted'
            or not isinstance(info.get('submission'), str)
            or not isinstance(runner.get('final_response'), str)
            or info.get('submission') != runner.get('final_response')):
        raise InvalidTerminal('native and runner termination differ')
    if info.get('config', {}).get('model', {}).get('model_name') != 'deepseek/deepseek-flash':
        raise InvalidTerminal('native configured model differs')
    messages = trajectory.get('messages')
    if (not isinstance(messages, list) or len(messages) < 4
            or messages[0].get('role') != 'system' or messages[1].get('role') != 'user'
            or messages[-2].get('role') != 'assistant' or messages[-1].get('role') != 'exit'):
        raise InvalidTerminal('ambiguous terminal frontier')
    template = info.get('config', {}).get('agent', {}).get('instance_template')
    if (not isinstance(template, str) or template.count('{{task}}') != 1
            or hashlib.sha256(template.encode()).hexdigest() != expected_template_sha256):
        raise InvalidTerminal('native task slot is ambiguous')
    prefix, suffix = template.split('{{task}}')
    # System fields/conditional examples are rendered by native Jinja. Do not
    # execute a serialized template on the host. Bind the exact task region
    # through the first later dynamic field, retaining the full raw prompt.
    fixed_suffix = re.split(r'\{[{%]', suffix, maxsplit=1)[0]
    expected_region = prefix + task + fixed_suffix
    content = messages[1].get('content')
    if (not isinstance(content, str) or not content.startswith(expected_region)
            or content.count(task) != 1):
        raise InvalidTerminal('native prompt differs from frozen task region')
    terminal = messages[-2]
    calls = terminal.get('tool_calls', [])
    if len(calls) != 1 or calls[0].get('type') != 'function':
        raise InvalidTerminal('terminal must contain exactly one native submit call')
    function = calls[0].get('function', {})
    if function.get('name') != 'bash':
        raise InvalidTerminal('terminal tool differs')
    try:
        arguments = json.loads(function.get('arguments', ''))
    except (TypeError, ValueError) as exc:
        raise InvalidTerminal('invalid terminal arguments') from exc
    if arguments != {'command': 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'}:
        raise InvalidTerminal('terminal command is not the native submit sentinel')
    exit_message = messages[-1]
    if (exit_message.get('extra', {}).get('exit_status') != 'Submitted'
            or exit_message.get('extra', {}).get('submission') != info.get('submission')
            or exit_message.get('content') != info.get('submission')):
        raise InvalidTerminal('exit evidence differs')
    pending = set()
    seen = set()
    submits = 0
    for message in messages[2:-1]:
        role = message.get('role')
        if role == 'assistant':
            if pending:
                raise InvalidTerminal('assistant advanced with unresolved earlier tools')
            choices = message.get('extra', {}).get('response', {}).get('choices', [])
            if (len(choices) != 1 or choices[0].get('message', {}).get('content') != message.get('content')
                    or choices[0].get('message', {}).get('tool_calls') != message.get('tool_calls')):
                raise InvalidTerminal('serialized assistant differs from native response')
            expected_actions = []
            for call in message.get('tool_calls', []):
                fn = call.get('function', {})
                if call.get('type') != 'function' or fn.get('name') != 'bash':
                    raise InvalidTerminal('unsupported native tool type')
                try:
                    args = json.loads(fn.get('arguments', ''))
                except (TypeError, ValueError) as exc:
                    raise InvalidTerminal('invalid native tool arguments') from exc
                if not isinstance(args, dict) or not isinstance(args.get('command'), str):
                    raise InvalidTerminal('native command missing')
                if args['command'] == 'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT':
                    submits += 1
                identifier = call.get('id')
                if not isinstance(identifier, str) or not identifier or identifier in seen:
                    raise InvalidTerminal('duplicate or missing native tool identity')
                pending.add(identifier)
                seen.add(identifier)
                expected_actions.append({'command': args['command'], 'tool_call_id': identifier})
            if message.get('extra', {}).get('actions') != expected_actions:
                raise InvalidTerminal('executed native actions differ from tool calls')
        elif role == 'tool':
            identifier = message.get('tool_call_id')
            if identifier not in pending:
                raise InvalidTerminal('unbound native tool observation')
            extra = message.get('extra', {})
            output = extra.get('raw_output')
            if not isinstance(output, str) or type(extra.get('returncode')) is not int:
                raise InvalidTerminal('native tool execution observation missing')
            lines = output.lstrip().splitlines()
            if lines and lines[0].strip() == 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' and extra['returncode'] == 0:
                raise InvalidTerminal('native submission occurred before claimed terminal')
            pending.remove(identifier)
        else:
            raise InvalidTerminal('unexpected user/exit/role before terminal')
    if pending != {calls[0].get('id')} or submits != 1:
        raise InvalidTerminal('terminal leaves other tools unresolved')
    summary = terminal.get('content')
    if not isinstance(summary, str) or not summary.strip():
        raise InvalidTerminal('terminal summary absent')
    return {'native_terminal_summary': summary,
            'summary_sha256': hashlib.sha256(summary.encode()).hexdigest(),
            'message_index': len(messages) - 2, 'submit_tool_call_id': calls[0]['id'],
            'method': 'assistant content accompanying sole native submit before Submitted exit',
            'original_submission': info['submission'], 'original_result_unchanged': True,
            'configured_model': 'deepseek/deepseek-flash',
            'prompt_binding': 'exact frozen task region; full rendered environment/template not attested',
            'template_sha256': expected_template_sha256,
            'observed_provider_model': 'NOT_VERIFIED_BY_THIS_CONSUMER',
            'summary_truthfulness': 'REQUIRES_SEPARATE_REVIEW',
            'semantic_acceptance': 'NOT_VERIFIED'}
