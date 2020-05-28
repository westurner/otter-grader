########################################################
##### Notebook and File Execution for Otter-Grader #####
########################################################

import argparse
import os
import re
import json
import ast
import itertools
import inspect
import nb2pdf
import pandas as pd

from glob import glob
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
from IPython import get_ipython
from IPython.display import display

from .ok_parser import OKTests, CheckCallWrapper
from .utils import hide_outputs, id_generator

try:
    from IPython.core.inputsplitter import IPythonInputSplitter
except ImportError:
    raise ImportError('IPython needs to be installed for notebook grading')


def check(test_file_path, global_env=None):
    """
    Checks ``global_env`` against given ``test_file`` in OK-format. If global_env is ``None``, the 
    global environment of the calling function is used. The following two calls are equivalent:

    .. code-block:: python
        check('tests/q1.py')
        check('tests/q1.py', globals())
    
    Args:
        test_file_path (``str``): path to test file
        global_env (``dict``, optional): a global environment resulting from the execution 
            of a python script or notebook

    Returns:
        ``otter.ok_parser.OKTestsResult``: result of running the tests in the given global environment

    """
    tests = OKTests([test_file_path])

    if global_env is None:
        # Get the global env of our callers - one level below us in the stack
        # The grade method should only be called directly from user / notebook
        # code. If some other method is calling it, it should also use the
        # inspect trick to pass in its parents' global env.
        global_env = inspect.currentframe().f_back.f_globals

    return tests.run(global_env, include_grade=False)


def grade_notebook(notebook_path, tests_glob=None, name=None, ignore_errors=True, script=False, 
    cwd=None, test_dir=None, seed=None, pregraded_results=[]):
    """
    Grade a notebook file & return grade information

    This function grades a single Jupyter notebook using the provided tests. If grading a Python file,
    set ``script`` to true. 

    Args:
        notebook_path (``str``): path to a single notebook
        tests_glob (``list`` of ``str``, optional): names of test files
        name (``str``, optional): initial environment name
        ignore_errors (``bool``, optional): whether errors in execution should be ignored
        script (``bool``, optional): whether the notebook_path is a Python script
        cwd (``str``, optional): working directory of execution to be appended to ``sys.path`` in 
            grading environment
        test_dir (``str``, optional): path to directory of tests in grading environment
        seed (``int``, optional): random seed for intercell seeding
        pregraded_results (``list`` of ``otter.ok_parser.OKTestResults``): a list of grading results
            for pregraded questions

    Returns:
        ``dict``: a score mapping with keys for each test, the student's scores, and total points 
            earned and possible 
    """
    # ensure this is not being executed inside a notebook
    assert get_ipython() is None, "Cannot execute inside Jupyter Notebook"

    if not script:
        try:
            with open(notebook_path) as f:
                nb = json.load(f)
        except UnicodeDecodeError:
            with open(notebook_path, encoding='utf-8') as f:
                nb = json.load(f)
    else:
        with open(notebook_path) as f:
            nb = f.read()

    secret = id_generator()
    results_array = "check_results_{}".format(secret)
    initial_env = {
        results_array: []
    }

    if name:
        initial_env["__name__"] = name

    if script:
        global_env = execute_script(nb, secret, initial_env, ignore_errors=ignore_errors, cwd=cwd, seed=seed)
    else:
        global_env = execute_notebook(nb, secret, initial_env, ignore_errors=ignore_errors, cwd=cwd, test_dir=test_dir, seed=seed)

    test_results = global_env[results_array]

    # Check for tests which were not included in the notebook and specified by tests_globs
    # Allows instructors to run notebooks with additional tests not accessible to user
    if tests_glob:
        # unpack list of paths into a single list
        tested_set = list(itertools.chain(*[r.paths for r in test_results]))
        extra_tests = []
        for t in sorted(tests_glob):
            include = True
            for tested in tested_set:
                if tested in t:     # e.g. if 'tests/q1.py' is in /srv/repo/lab01/tests/q1.py'
                    include = False
            if include:
                extra_tests.append(OKTests([t]))
        extra_results = [t.run(global_env, include_grade=False) for t in extra_tests]
        test_results += extra_results

    if pregraded_results:
        # test_results += pregraded_results

        tested_set = list(itertools.chain(*[r.paths for r in test_results]))
        for r in pregraded_results:
            removal_indices = []
            for i, tested in enumerate(tested_set):
                if any([tested in path for path in r.paths]):     # e.g. if 'tests/q1.py' is in /srv/repo/lab01/tests/q1.py'
                    removal_indices.append(i)
            removal_indices.reverse()
            for i in removal_indices:
                del test_results[i]

        test_results += pregraded_results

    score_mapping = {}
    points_possible, total_score = 0, 0
    for r in test_results:
        try:
            for test in r.tests:
                test_name = os.path.split(test.name)[1][:-3]
                score_mapping[test_name] = {
                    "score": r.grade * test.value,
                    "possible": test.value,
                    # "hidden": test.hidden
                }
                total_score += r.grade * test.value
                points_possible += test.value
            for tup in r.failed_tests:
                test_name = os.path.split(tup[0].name)[1][:-3]
                if test_name in score_mapping:
                    score_mapping[test_name]["hint"] = tup[1]#.__repr__()
                    score_mapping[test_name]["hidden"] = tup[1].failed_test_hidden
                else:
                    score_mapping[test_name] = {
                        "hint": tup[1],#.__repr__()
                        "hidden": tup[1].failed_test_hidden
                    }
        except IndexError:
            pass

    # add in total score and avoid divide by zero error if there are no tests
    score_mapping["total"] = total_score
    score_mapping["possible"] = points_possible

    return score_mapping

def grade(ipynb_path, pdf, tag_filter, html_filter, script, ignore_errors=True, seed=None, cwd=None):
    """
    Grades a single ipython notebook and returns the score

    If no PDF is needed, set the pdf, tag_filter, and html_filter parameters to false. For .py
    files, set script to true.

    Args:
        ipynb_path (``str``): path to the notebook
        pdf (``bool``): whether unfiltered PDFs of notebooks should be generated
        tag_filter (``bool``): whether cell tag-filtered PDFs of notebooks should be generated
        html_filter (``bool``): whether HTML comment-filtered PDFs of notebooks should be generated
        script (``bool``): whether the input file is a Python script
        ignore_errors (``bool``, optional): whether errors should be ignored during execution
        seed (``int``, optional): random seed for intercell seeding
        cwd (``str``, optional): working directory of execution to be appended to ``sys.path`` in 
            grading environment

    Returns:
        ``dict``: a score mapping with keys for each test, the student's scores, and total points 
            earned and possible 
    """
    # # get path of notebook file
    # base_path = os.path.dirname(ipynb_path)

    # glob tests
    test_files = glob('/home/tests/*.py')

    # get score
    result = grade_notebook(ipynb_path, test_files, script=script, ignore_errors=ignore_errors, seed=seed, cwd=cwd)

    # output PDF
    if pdf:
        nb2pdf.convert(ipynb_path)
    elif tag_filter:
        nb2pdf.convert(ipynb_path, filtering=True, filter_type="tags")
    elif html_filter:
        nb2pdf.convert(ipynb_path, filtering=True, filter_type="html")

    return result

def execute_notebook(nb, secret='secret', initial_env=None, ignore_errors=False, cwd=None, test_dir=None, seed=None):
    """
    Executes a notebook and returns the global environment that results from execution

    Execute notebook & return the global environment that results from execution. If ``ignore_errors`` 
    is ``True``, exceptions are swallowed. ``secret`` contains random digits so ``check_results`` and 
    ``check`` are not easily modifiable. ``nb`` is passed in as a dictionary that's a parsed notebook

    Args:
        nb (``dict``): JSON representation of a notebook
        secret (``str``, optional): randomly generated integer used to rebind check function
        initial_env (``str``, optional): name of initial environment
        ignore_errors (``bool``, optional): whether exceptions should be ignored
        cwd (``str``, optional): working directory of execution to be appended to ``sys.path`` in 
            grading environment
        test_dir (``str``, optional): path to directory of tests in grading environment
        seed (``int``, optional): random seed for intercell seeding
    
    Results:
        ``dict``: global environment resulting from executing all code of the input notebook
    """
    with hide_outputs():
        if initial_env:
            global_env = initial_env.copy()
        else:
            global_env = {}

        source = ""
        # if gradescope:
        #     source = "import sys\nsys.path.append(\"/autograder/submission\")\n"
        # el
        if cwd:
            source =  f"import sys\nsys.path.append(\"{cwd}\")\n"
        if seed is not None:
            # source += "import numpy as np\nimport random\n"
            import numpy as np
            import random
            global_env["np"] = np
            global_env["random"] = random

        # Before rewriting AST, find cells of code that generate errors.
        # One round of execution is done beforehand to mimic the Jupyter notebook style of running
        # (e.g. code runs up to the point of execution).
        # The reason this is workaround is introduced is because once the
        # source code is parsed into an AST, there is no sense of local cells

        for cell in nb['cells']:
            if cell['cell_type'] == 'code':
                # transform the input to executable Python
                # FIXME: use appropriate IPython functions here
                isp = IPythonInputSplitter(line_input_checker=False)
                try:
                    code_lines = []
                    cell_source_lines = cell['source']
                    source_is_str_bool = False
                    if isinstance(cell_source_lines, str):
                        source_is_str_bool = True
                        cell_source_lines = cell_source_lines.split('\n')

                    for line in cell_source_lines:
                        # Filter out ipython magic commands
                        # Filter out interact widget
                        if not line.startswith('%'):
                            if "interact(" not in line and not re.search(r"otter\.Notebook\(.*?\)", line):
                                code_lines.append(line)
                                if source_is_str_bool:
                                    code_lines.append('\n')
                            elif re.search(r"otter\.Notebook\(.*?\)", line):
                                # TODO: move this check into CheckCallWrapper
                                # if gradescope:
                                #     line = re.sub(r"otter\.Notebook\(.*?\)", "otter.Notebook(\"/autograder/submission/tests\")", line)
                                # el
                                if test_dir:
                                    line = re.sub(r"otter\.Notebook\(.*?\)", f"otter.Notebook(\"{test_dir}\")", line)
                                else:
                                    line = re.sub(r"otter\.Notebook\(.*?\)", "otter.Notebook(\"/home/tests\")", line)
                                code_lines.append(line)
                                if source_is_str_bool:
                                    code_lines.append('\n')
                    if seed is not None:
                        cell_source = "np.random.seed({})\nrandom.seed({})\n".format(seed, seed) + isp.transform_cell(''.join(code_lines))
                    else:
                        cell_source = isp.transform_cell(''.join(code_lines))

                    # patch otter.Notebook.export so that we don't create PDFs in notebooks
                    # TODO: move this patch into CheckCallWrapper
                    m = mock.mock_open()
                    with mock.patch('otter.Notebook.export', m):
                        exec(cell_source, global_env)
                    source += cell_source
                except:
                    if not ignore_errors:
                        raise

        tree = ast.parse(source)
        # # CODE BELOW COMMENTED OUT BECAUSE the only check function is within the Notebook class
        # if find_check_assignment(tree) or find_check_definition(tree):
        #     # an empty global_env will fail all the tests
        #     return global_env

        # wrap check(..) calls into a check_results_X.append(check(..))
        transformer = CheckCallWrapper(secret)
        tree = transformer.visit(tree)
        ast.fix_missing_locations(tree)

        cleaned_source = compile(tree, filename="nb-ast", mode="exec")
        try:
            with open(os.devnull, 'w') as f, redirect_stdout(f), redirect_stderr(f):
                # patch otter.Notebook.export so that we don't create PDFs in notebooks
                m = mock.mock_open()
                with mock.patch('otter.Notebook.export', m):
                    exec(cleaned_source, global_env)
        except:
            if not ignore_errors:
                raise
        return global_env

def execute_script(script, secret='secret', initial_env=None, ignore_errors=False, cwd=None, test_dir=None, seed=None):
    """
    Executes code of a Python script and returns the resulting global environment

    Execute script & return the global environment that results from execution. If ``ignore_errors`` is 
    ``True``, exceptions are swallowed. ``secret`` contains random digits so ``check_results`` and 
    ``check`` are not easily modifiable. ``script`` is passed in as a string.

    Args:
        script (``str``): string representation of Python script code
        secret (``str``, optional): randomly generated integer used to rebind check function
        initial_env (``str``, optional): name of initial environment
        ignore_errors (``bool``, optional): whether exceptions should be ignored
        cwd (``str``, optional): working directory of execution to be appended to ``sys.path`` in 
            grading environment
        test_dir (``str``, optional): path to directory of tests in grading environment
        seed (``int``, optional): random seed for intercell seeding
    
    Results:
        dict: global environment resulting from executing all code of the input script
    """
    with hide_outputs():
        if initial_env:
            global_env = initial_env.copy()
        else:
            global_env = {}
        source = ""
        # if gradescope:
        #     source = "import sys\nsys.path.append(\"/autograder/submission\")\n"
        # el
        if cwd:
            source =  f"import sys\nsys.path.append(\"{cwd}\")\n"
        if seed is not None:
            # source += "import numpy as np\nimport random\n"
            import numpy as np
            import random
            global_env["np"] = np
            global_env["random"] = random
            source += "np.random.seed({})\nrandom.seed({})\n".format(seed, seed)

        lines = script.split("\n")
        for i, line in enumerate(lines):
            # TODO: move this check into CheckCallWrapper
            if re.search(r"otter\.Notebook\(.*?\)", line):
                # if gradescope:
                #     line = re.sub(r"otter\.Notebook\(.*?\)", "otter.Notebook(\"/autograder/submission/tests\")", line)
                # else:
                if test_dir:
                    line = re.sub(r"otter\.Notebook\(.*?\)", f"otter.Notebook(\"{test_dir}\")", line)
                else:
                    line = re.sub(r"otter\.Notebook\(.*?\)", "otter.Notebook(\"/home/tests\")", line)
            lines[i] = line
        try:
            exec("\n".join(lines), global_env)
            source += "\n".join(lines)
        except:
            if not ignore_errors:
                raise
        
        tree = ast.parse(source)
        # # CODE BELOW COMMENTED OUT BECAUSE the only check function is within the Notebook class
        # if find_check_assignment(tree) or find_check_definition(tree):
        #     # an empty global_env will fail all the tests
        #     return global_env

        # wrap check(..) calls into a check_results_X.append(check(..))
        transformer = CheckCallWrapper(secret)
        tree = transformer.visit(tree)
        ast.fix_missing_locations(tree)
        cleaned_source = compile(tree, filename="nb-ast", mode="exec")
        try:
            with open(os.devnull, 'w') as f, redirect_stdout(f), redirect_stderr(f):
                exec(cleaned_source, global_env)
        except:
            if not ignore_errors:
                raise
        return global_env


def main(args=None):
    """
    Parses command line arguments and executes submissions. Writes grades to a CSV file and optionally
    generates PDFs of submissions.

    Args:
        args (``list`` of ``str``, optional): alternate command line arguments
    """
    # implement argparser
    parser = argparse.ArgumentParser()
    parser.add_argument('notebook_directory', help='Path to directory with ipynb\'s to grade')
    parser.add_argument("--pdf", action="store_true", default=False)
    parser.add_argument("--tag-filter", action="store_true", default=False)
    parser.add_argument("--html-filter", action="store_true", default=False)
    parser.add_argument("--scripts", action="store_true", default=False)
    parser.add_argument("--seed", type=int, default=None, help="A random seed to be executed before each cell")
    parser.add_argument("--verbose", default=False, action="store_true", help="If present prints scores and hints to stdout")
    parser.add_argument("--debug", default=False, action="store_true", help="Does not ignore errors on execution")

    if args is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args(args)

    # get all ipynb files
    dir_path = os.path.abspath(args.notebook_directory)
    os.chdir(dir_path)
    file_extension = (".py", ".ipynb")[not args.scripts]
    all_ipynb = [(f, os.path.join(dir_path, f)) for f in os.listdir(dir_path) if os.path.isfile(os.path.join(dir_path, f)) and f.endswith(file_extension)]

    all_results = {"file": [], "score": [], "manual": []}

    if not args.pdf and not args.html_filter and not args.tag_filter:
        del all_results["manual"]

    for ipynb_name, ipynb_path in all_ipynb:
        all_results["file"].append(ipynb_name)
        score = grade(ipynb_path, args.pdf, args.tag_filter, args.html_filter, args.scripts, ignore_errors=not args.debug, seed=args.seed, cwd=dir_path)
        if args.verbose:
            print("Score details for {}".format(ipynb_name))
            print(json.dumps(score, default=lambda o: repr(o)))
        # del score["TEST_HINTS"]
        all_results["score"].append({t : score[t]["score"] if type(score[t]) == dict else score[t] for t in score})
        if args.pdf or args.html_filter or args.tag_filter:
            pdf_path = re.sub(r"\.ipynb$", ".pdf", ipynb_path)
            all_results["manual"].append(pdf_path)

    try:
        # expand mappings in all_results["score"]
        for q in all_results["score"][0].keys():
            all_results[q] = []

        for score in all_results["score"]:
            for q in score:
                all_results[q] += [score[q]]#["score"]]

    except IndexError:
        pass

    del all_results["score"]

    final_results = pd.DataFrame(all_results)
    final_results.to_csv("grades.csv", index=False)


if __name__ == "__main__":
    main()
