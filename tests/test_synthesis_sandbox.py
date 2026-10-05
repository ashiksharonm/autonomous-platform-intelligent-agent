import pytest

from agent.synthesis import SynthesisError, validate_source


def test_good_code_passes():
    script_text = '''
def run(repo, params: dict) -> dict:
    import datetime
    open_bugs = list(repo.get_issues(state="open"))
    return {"summary": str(len(open_bugs)) + " open issues", "actions_taken": []}
'''
    validate_source(script_text)


def test_bad_params_fails():
    script_text = "def run(first_arg, second_arg):\n    return {}\n"
    with pytest.raises(SynthesisError):
        validate_source(script_text)


def test_illegal_import_fails():
    script_text = '''
import os
def run(repo, params: dict) -> dict:
    return {"summary": "done", "actions_taken": []}
'''
    with pytest.raises(SynthesisError):
        validate_source(script_text)


def test_eval_use_fails():
    script_text = '''
def run(repo, params: dict) -> dict:
    eval("1+1")
    return {"summary": "done", "actions_taken": []}
'''
    with pytest.raises(SynthesisError):
        validate_source(script_text)


def test_os_module_use_fails():
    script_text = '''
def run(repo, params: dict) -> dict:
    os.system("echo hi")
    return {"summary": "done", "actions_taken": []}
'''
    with pytest.raises(SynthesisError):
        validate_source(script_text)


def test_dunder_methods_fails():
    script_text = '''
def run(repo, params: dict) -> dict:
    secret_data = repo.__class__.__bases__
    return {"summary": "done", "actions_taken": []}
'''
    with pytest.raises(SynthesisError):
        validate_source(script_text)


def test_broken_syntax_fails():
    with pytest.raises(SynthesisError):
        validate_source("def run(repo, params:\n    pass")