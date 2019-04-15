from collections import OrderedDict
import io
import os
from typing import Any, List, Optional, Set, Tuple, Union, TYPE_CHECKING  # noqa F401
import warnings

import numpy as np
import sklearn.metrics
import xmltodict

import openml
import openml.utils
import openml._api_calls
from openml.exceptions import PyOpenMLError
from openml.extensions import get_extension_by_model
from openml import config
from openml.flows.flow import _copy_server_fields
from ..flows import get_flow, flow_exists, OpenMLFlow
from ..setups import setup_exists, initialize_model
from ..exceptions import OpenMLCacheException, OpenMLServerException, OpenMLRunsExistError
from ..tasks import OpenMLTask, OpenMLClassificationTask, OpenMLClusteringTask, \
    OpenMLRegressionTask, OpenMLSupervisedTask, OpenMLLearningCurveTask
from .run import OpenMLRun
from .trace import OpenMLRunTrace
from ..tasks import TaskTypeEnum

# Avoid import cycles: https://mypy.readthedocs.io/en/latest/common_issues.html#import-cycles
if TYPE_CHECKING:
    from openml.extensions.extension_interface import Extension

# get_dict is in run.py to avoid circular imports

RUNS_CACHE_DIR_NAME = 'runs'


def run_model_on_task(
    model: Any,
    task: OpenMLTask,
    avoid_duplicate_runs: bool = True,
    flow_tags: List[str] = None,
    seed: int = None,
    add_local_measures: bool = True,
    upload_flow: bool = False,
    return_flow: bool = False,
) -> Union[OpenMLRun, Tuple[OpenMLRun, OpenMLFlow]]:
    """Run the model on the dataset defined by the task.

    Parameters
    ----------
    model : sklearn model
        A model which has a function fit(X,Y) and predict(X),
        all supervised estimators of scikit learn follow this definition of a model [1]
        [1](http://scikit-learn.org/stable/tutorial/statistical_inference/supervised_learning.html)
    task : OpenMLTask
        Task to perform. This may be a model instead if the first argument is an OpenMLTask.
    avoid_duplicate_runs : bool, optional (default=True)
        If True, the run will throw an error if the setup/task combination is already present on
        the server. This feature requires an internet connection.
    flow_tags : List[str], optional (default=None)
        A list of tags that the flow should have at creation.
    seed: int, optional (default=None)
        Models that are not seeded will get this seed.
    add_local_measures : bool, optional (default=True)
        Determines whether to calculate a set of evaluation measures locally,
        to later verify server behaviour.
    upload_flow : bool (default=False)
        If True, upload the flow to OpenML if it does not exist yet.
        If False, do not upload the flow to OpenML.
    return_flow : bool (default=False)
        If True, returns the OpenMLFlow generated from the model in addition to the OpenMLRun.

    Returns
    -------
    run : OpenMLRun
        Result of the run.
    flow : OpenMLFlow (optional, only if `return_flow` is True).
        Flow generated from the model.
    """

    extension = get_extension_by_model(model, raise_if_no_extension=True)
    if extension is None:
        # This should never happen and is only here to please mypy will be gone soon once the
        # whole function is removed
        raise TypeError(extension)

    # TODO: At some point in the future do not allow for arguments in old order (6-2018).
    # Flexibility currently still allowed due to code-snippet in OpenML100 paper (3-2019).
    # When removing this please also remove the method `is_estimator` from the extension
    # interface as it is only used here (MF, 3-2019)
    if isinstance(model, OpenMLTask) and extension.is_estimator(model):
        warnings.warn("The old argument order (task, model) is deprecated and "
                      "will not be supported in the future. Please use the "
                      "order (model, task).", DeprecationWarning)
        task, model = model, task

    flow = extension.model_to_flow(model)

    run = run_flow_on_task(
        task=task,
        flow=flow,
        avoid_duplicate_runs=avoid_duplicate_runs,
        flow_tags=flow_tags,
        seed=seed,
        add_local_measures=add_local_measures,
        upload_flow=upload_flow,
    )
    if return_flow:
        return run, flow
    return run


def run_flow_on_task(
    flow: OpenMLFlow,
    task: OpenMLTask,
    avoid_duplicate_runs: bool = True,
    flow_tags: List[str] = None,
    seed: int = None,
    add_local_measures: bool = True,
    upload_flow: bool = False,
) -> OpenMLRun:

    """Run the model provided by the flow on the dataset defined by task.

    Takes the flow and repeat information into account.
    The Flow may optionally be published.

    Parameters
    ----------
    flow : OpenMLFlow
        A flow wraps a machine learning model together with relevant information.
        The model has a function fit(X,Y) and predict(X),
        all supervised estimators of scikit learn follow this definition of a model [1]
        [1](http://scikit-learn.org/stable/tutorial/statistical_inference/supervised_learning.html)
    task : OpenMLTask
        Task to perform. This may be an OpenMLFlow instead if the first argument is an OpenMLTask.
    avoid_duplicate_runs : bool, optional (default=True)
        If True, the run will throw an error if the setup/task combination is already present on
        the server. This feature requires an internet connection.
    avoid_duplicate_runs : bool, optional (default=True)
        If True, the run will throw an error if the setup/task combination is already present on
        the server. This feature requires an internet connection.
    flow_tags : List[str], optional (default=None)
        A list of tags that the flow should have at creation.
    seed: int, optional (default=None)
        Models that are not seeded will get this seed.
    add_local_measures : bool, optional (default=True)
        Determines whether to calculate a set of evaluation measures locally,
        to later verify server behaviour.
    upload_flow : bool (default=False)
        If True, upload the flow to OpenML if it does not exist yet.
        If False, do not upload the flow to OpenML.

    Returns
    -------
    run : OpenMLRun
        Result of the run.
    """
    if flow_tags is not None and not isinstance(flow_tags, list):
        raise ValueError("flow_tags should be a list")

    # TODO: At some point in the future do not allow for arguments in old order (changed 6-2018).
    # Flexibility currently still allowed due to code-snippet in OpenML100 paper (3-2019).
    if isinstance(flow, OpenMLTask) and isinstance(task, OpenMLFlow):
        # We want to allow either order of argument (to avoid confusion).
        warnings.warn("The old argument order (Flow, model) is deprecated and "
                      "will not be supported in the future. Please use the "
                      "order (model, Flow).", DeprecationWarning)
        task, flow = flow, task

    flow.model = flow.extension.seed_model(flow.model, seed=seed)

    # We only need to sync with the server right now if we want to upload the flow,
    # or ensure no duplicate runs exist. Otherwise it can be synced at upload time.
    flow_id = None
    if upload_flow or avoid_duplicate_runs:
        flow_id = flow_exists(flow.name, flow.external_version)
        if isinstance(flow.flow_id, int) and flow_id != flow.flow_id:
            if flow_id:
                raise PyOpenMLError("Local flow_id does not match server flow_id: "
                                    "'{}' vs '{}'".format(flow.flow_id, flow_id))
            else:
                raise PyOpenMLError("Flow does not exist on the server, "
                                    "but 'flow.flow_id' is not None.")

        if upload_flow and not flow_id:
            flow.publish()
            flow_id = flow.flow_id
        elif flow_id:
            flow_from_server = get_flow(flow_id)
            _copy_server_fields(flow_from_server, flow)
            if avoid_duplicate_runs:
                flow_from_server.model = flow.model
                setup_id = setup_exists(flow_from_server)
                ids = run_exists(task.task_id, setup_id)
                if ids:
                    error_message = ("One or more runs of this setup were "
                                     "already performed on the task.")
                    raise OpenMLRunsExistError(ids, error_message)
        else:
            # Flow does not exist on server and we do not want to upload it.
            # No sync with the server happens.
            flow_id = None
            pass

    dataset = task.get_dataset()

    run_environment = flow.extension.get_version_information()
    tags = ['openml-python', run_environment[1]]

    # execute the run
    res = _run_task_get_arffcontent(
        model=flow.model,
        task=task,
        extension=flow.extension,
        add_local_measures=add_local_measures,
    )

    data_content, trace, fold_evaluations, sample_evaluations = res

    run = OpenMLRun(
        task_id=task.task_id,
        flow_id=flow_id,
        dataset_id=dataset.dataset_id,
        model=flow.model,
        flow_name=flow.name,
        tags=tags,
        trace=trace,
        data_content=data_content,
        flow=flow,
        setup_string=flow.extension.create_setup_string(flow.model),
    )

    if (upload_flow or avoid_duplicate_runs) and flow.flow_id is not None:
        # We only extract the parameter settings if a sync happened with the server.
        # I.e. when the flow was uploaded or we found it in the avoid_duplicate check.
        # Otherwise, we will do this at upload time.
        run.parameter_settings = flow.extension.obtain_parameter_values(flow)

    # now we need to attach the detailed evaluations
    if task.task_type_id == TaskTypeEnum.LEARNING_CURVE:
        run.sample_evaluations = sample_evaluations
    else:
        run.fold_evaluations = fold_evaluations

    if flow_id:
        message = 'Executed Task {} with Flow id:{}'.format(task.task_id, run.flow_id)
    else:
        message = 'Executed Task {} on local Flow with name {}.'.format(task.task_id, flow.name)
    config.logger.info(message)

    return run


def get_run_trace(run_id: int) -> OpenMLRunTrace:
    """
    Get the optimization trace object for a given run id.

    Parameters
    ----------
    run_id : int

    Returns
    -------
    openml.runs.OpenMLTrace
    """
    trace_xml = openml._api_calls._perform_api_call('run/trace/%d' % run_id,
                                                    'get')
    run_trace = OpenMLRunTrace.trace_from_xml(trace_xml)
    return run_trace


def initialize_model_from_run(run_id: int) -> Any:
    """
    Initialized a model based on a run_id (i.e., using the exact
    same parameter settings)

    Parameters
    ----------
    run_id : int
        The Openml run_id

    Returns
    -------
    model
    """
    run = get_run(run_id)
    return initialize_model(run.setup_id)


def initialize_model_from_trace(
    run_id: int,
    repeat: int,
    fold: int,
    iteration: Optional[int] = None,
) -> Any:
    """
    Initialize a model based on the parameters that were set
    by an optimization procedure (i.e., using the exact same
    parameter settings)

    Parameters
    ----------
    run_id : int
        The Openml run_id. Should contain a trace file,
        otherwise a OpenMLServerException is raised

    repeat : int
        The repeat nr (column in trace file)

    fold : int
        The fold nr (column in trace file)

    iteration : int
        The iteration nr (column in trace file). If None, the
        best (selected) iteration will be searched (slow),
        according to the selection criteria implemented in
        OpenMLRunTrace.get_selected_iteration

    Returns
    -------
    model
    """
    run = get_run(run_id)
    flow = get_flow(run.flow_id)
    run_trace = get_run_trace(run_id)

    if iteration is None:
        iteration = run_trace.get_selected_iteration(repeat, fold)

    request = (repeat, fold, iteration)
    if request not in run_trace.trace_iterations:
        raise ValueError('Combination repeat, fold, iteration not available')
    current = run_trace.trace_iterations[(repeat, fold, iteration)]

    search_model = initialize_model_from_run(run_id)
    model = flow.extension.instantiate_model_from_hpo_class(search_model, current)
    return model


def run_exists(task_id: int, setup_id: int) -> Set[int]:
    """Checks whether a task/setup combination is already present on the
    server.

    Parameters
    ----------
    task_id : int

    setup_id : int

    Returns
    -------
        Set run ids for runs where flow setup_id was run on task_id. Empty
        set if it wasn't run yet.
    """
    if setup_id <= 0:
        # openml setups are in range 1-inf
        return set()

    try:
        result = list_runs(task=[task_id], setup=[setup_id])
        if len(result) > 0:
            return set(result.keys())
        else:
            return set()
    except OpenMLServerException as exception:
        # error code 512 implies no results. The run does not exist yet
        assert (exception.code == 512)
        return set()


def _run_task_get_arffcontent(
    model: Any,
    task: OpenMLTask,
    extension: 'Extension',
    add_local_measures: bool,
) -> Tuple[
    List[List],
    Optional[OpenMLRunTrace],
    'OrderedDict[str, OrderedDict]',
    'OrderedDict[str, OrderedDict]',
]:
    arff_datacontent = []  # type: List[List]
    arff_tracecontent = []  # type: List[List]
    # stores fold-based evaluation measures. In case of a sample based task,
    # this information is multiple times overwritten, but due to the ordering
    # of tne loops, eventually it contains the information based on the full
    # dataset size
    user_defined_measures_per_fold = OrderedDict()  # type: 'OrderedDict[str, OrderedDict]'
    # stores sample-based evaluation measures (sublevel of fold-based)
    # will also be filled on a non sample-based task, but the information
    # is the same as the fold-based measures, and disregarded in that case
    user_defined_measures_per_sample = OrderedDict()  # type: 'OrderedDict[str, OrderedDict]'

    # TODO use different iterator to only provide a single iterator (less
    # methods, less maintenance, less confusion)
    num_reps, num_folds, num_samples = task.get_split_dimensions()
    n_classes = None

    for rep_no in range(num_reps):
        for fold_no in range(num_folds):
            for sample_no in range(num_samples):

                train_indices, test_indices = task.get_train_test_split_indices(
                    repeat=rep_no, fold=fold_no, sample=sample_no)
                if isinstance(task, OpenMLSupervisedTask):
                    x, y = task.get_X_and_y()
                    train_x = x[train_indices]
                    train_y = y[train_indices]
                    test_x = x[test_indices]
                    test_y = y[test_indices]
                    if isinstance(task, (OpenMLClassificationTask, OpenMLClassificationTask)):
                        n_classes = len(task.class_labels)
                elif isinstance(task, OpenMLClusteringTask):
                    train_x = train_indices
                    train_y = None
                    test_x = test_indices
                    test_y = None
                else:
                    raise NotImplementedError(task.task_type)

                (
                    pred_y,
                    proba_y,
                    user_defined_measures_fold,
                    model_fold,
                ) = extension._run_model_on_fold(
                    model=model,
                    task=task,
                    X_train=train_x,
                    y_train=train_y,
                    rep_no=rep_no,
                    fold_no=fold_no,
                    sample_no=sample_no,
                    add_local_measures=add_local_measures,
                    X_test=test_x,
                    n_classes=n_classes,
                )

                arff_datacontent_fold = []  # type: List[List]
                # extract trace, if applicable
                arff_tracecontent_fold = []  # type: List[List]
                if extension.is_hpo_class(model_fold):
                    arff_tracecontent_fold.extend(
                        extension._extract_trace_data(model_fold, rep_no, fold_no)
                    )

                # add client-side calculated metrics. These is used on the server as
                # consistency check, only useful for supervised tasks
                def _calculate_local_measure(sklearn_fn, openml_name):
                    user_defined_measures_fold[openml_name] = sklearn_fn(test_y, pred_y)

                if isinstance(task, (OpenMLClassificationTask, OpenMLLearningCurveTask)):

                    for i in range(0, len(test_indices)):

                        arff_line = [rep_no, fold_no, sample_no, i]  # type: List[Any]
                        for j, class_label in enumerate(task.class_labels):
                            arff_line.append(proba_y[i][j])

                        arff_line.append(task.class_labels[pred_y[i]])
                        arff_line.append(task.class_labels[test_y[i]])

                        arff_datacontent.append(arff_line)

                    if add_local_measures:
                        _calculate_local_measure(
                            sklearn.metrics.accuracy_score,
                            'predictive_accuracy',
                        )

                elif isinstance(task, OpenMLRegressionTask):

                    for i in range(0, len(test_indices)):
                        arff_line = [rep_no, fold_no, test_indices[i], pred_y[i], test_y[i]]
                        arff_datacontent.append(arff_line)

                    if add_local_measures:
                        _calculate_local_measure(
                            sklearn.metrics.mean_absolute_error,
                            'mean_absolute_error',
                        )

                elif isinstance(task, OpenMLClusteringTask):
                    for i in range(0, len(test_indices)):
                        arff_line = [test_indices[i], pred_y[i]]  # row_id, cluster ID
                        arff_datacontent.append(arff_line)

                else:
                    raise TypeError(type(task))

                arff_datacontent.extend(arff_datacontent_fold)
                arff_tracecontent.extend(arff_tracecontent_fold)

                for measure in user_defined_measures_fold:

                    if measure not in user_defined_measures_per_fold:
                        user_defined_measures_per_fold[measure] = OrderedDict()
                    if rep_no not in user_defined_measures_per_fold[measure]:
                        user_defined_measures_per_fold[measure][rep_no] = OrderedDict()

                    if measure not in user_defined_measures_per_sample:
                        user_defined_measures_per_sample[measure] = OrderedDict()
                    if rep_no not in user_defined_measures_per_sample[measure]:
                        user_defined_measures_per_sample[measure][rep_no] = OrderedDict()
                    if fold_no not in user_defined_measures_per_sample[
                            measure][rep_no]:
                        user_defined_measures_per_sample[measure][rep_no][fold_no] = OrderedDict()

                    user_defined_measures_per_fold[measure][rep_no][
                        fold_no] = user_defined_measures_fold[measure]
                    user_defined_measures_per_sample[measure][rep_no][fold_no][
                        sample_no] = user_defined_measures_fold[measure]

    # Note that we need to use a fitted model (i.e., model_fold, and not model)
    # here, to ensure it contains the hyperparameter data (in cv_results_)
    if extension.is_hpo_class(model):
        trace = extension.obtain_arff_trace(model_fold, arff_tracecontent)  # type: Optional[OpenMLRunTrace]  # noqa E501
    else:
        trace = None

    return (
        arff_datacontent,
        trace,
        user_defined_measures_per_fold,
        user_defined_measures_per_sample,
    )


def get_runs(run_ids):
    """Gets all runs in run_ids list.

    Parameters
    ----------
    run_ids : list of ints

    Returns
    -------
    runs : list of OpenMLRun
        List of runs corresponding to IDs, fetched from the server.
    """

    runs = []
    for run_id in run_ids:
        runs.append(get_run(run_id))
    return runs


@openml.utils.thread_safe_if_oslo_installed
def get_run(run_id):
    """Gets run corresponding to run_id.

    Parameters
    ----------
    run_id : int

    Returns
    -------
    run : OpenMLRun
        Run corresponding to ID, fetched from the server.
    """
    run_dir = openml.utils._create_cache_directory_for_id(RUNS_CACHE_DIR_NAME,
                                                          run_id)
    run_file = os.path.join(run_dir, "description.xml")

    if not os.path.exists(run_dir):
        os.makedirs(run_dir)

    try:
        return _get_cached_run(run_id)

    except (OpenMLCacheException):
        run_xml = openml._api_calls._perform_api_call("run/%d" % run_id,
                                                      'get')
        with io.open(run_file, "w", encoding='utf8') as fh:
            fh.write(run_xml)

    run = _create_run_from_xml(run_xml)

    return run


def _create_run_from_xml(xml, from_server=True):
    """Create a run object from xml returned from server.

    Parameters
    ----------
    xml : string
        XML describing a run.

    from_server : bool, optional (default=True)
        If True, an AttributeError is raised if any of the fields required by the server is not
        present in the xml. If False, those absent fields will be treated as None.

    Returns
    -------
    run : OpenMLRun
        New run object representing run_xml.
    """

    def obtain_field(xml_obj, fieldname, from_server, cast=None):
        # this function can be used to check whether a field is present in an
        # object. if it is not present, either returns None or throws an error
        # (this is usually done if the xml comes from the server)
        if fieldname in xml_obj:
            if cast is not None:
                return cast(xml_obj[fieldname])
            return xml_obj[fieldname]
        elif not from_server:
            return None
        else:
            raise AttributeError('Run XML does not contain required (server) '
                                 'field: ', fieldname)

    run = xmltodict.parse(xml, force_list=['oml:file', 'oml:evaluation',
                                           'oml:parameter_setting'])["oml:run"]
    run_id = obtain_field(run, 'oml:run_id', from_server, cast=int)
    uploader = obtain_field(run, 'oml:uploader', from_server, cast=int)
    uploader_name = obtain_field(run, 'oml:uploader_name', from_server)
    task_id = int(run['oml:task_id'])
    task_type = obtain_field(run, 'oml:task_type', from_server)

    # even with the server requirement this field may be empty.
    if 'oml:task_evaluation_measure' in run:
        task_evaluation_measure = run['oml:task_evaluation_measure']
    else:
        task_evaluation_measure = None

    if not from_server and run['oml:flow_id'] is None:
        # This can happen for a locally stored run of which the flow is not yet published.
        flow_id = None
        parameters = None
    else:
        flow_id = obtain_field(run, 'oml:flow_id', from_server, cast=int)
        # parameters are only properly formatted once the flow is established on the server.
        # thus they are also not stored for runs with local flows.
        parameters = []
        if 'oml:parameter_setting' in run:
            obtained_parameter_settings = run['oml:parameter_setting']
            for parameter_dict in obtained_parameter_settings:
                current_parameter = OrderedDict()
                current_parameter['oml:name'] = parameter_dict['oml:name']
                current_parameter['oml:value'] = parameter_dict['oml:value']
                if 'oml:component' in parameter_dict:
                    current_parameter['oml:component'] = \
                        parameter_dict['oml:component']
                parameters.append(current_parameter)

    flow_name = obtain_field(run, 'oml:flow_name', from_server)
    setup_id = obtain_field(run, 'oml:setup_id', from_server, cast=int)
    setup_string = obtain_field(run, 'oml:setup_string', from_server)

    if 'oml:input_data' in run:
        dataset_id = int(run['oml:input_data']['oml:dataset']['oml:did'])
    elif not from_server:
        dataset_id = None

    files = OrderedDict()
    evaluations = OrderedDict()
    fold_evaluations = OrderedDict()
    sample_evaluations = OrderedDict()
    if 'oml:output_data' not in run:
        if from_server:
            raise ValueError('Run does not contain output_data '
                             '(OpenML server error?)')
    else:
        output_data = run['oml:output_data']
        if 'oml:file' in output_data:
            # multiple files, the normal case
            for file_dict in output_data['oml:file']:
                files[file_dict['oml:name']] = int(file_dict['oml:file_id'])
        if 'oml:evaluation' in output_data:
            # in normal cases there should be evaluations, but in case there
            # was an error these could be absent
            for evaluation_dict in output_data['oml:evaluation']:
                key = evaluation_dict['oml:name']
                if 'oml:value' in evaluation_dict:
                    value = float(evaluation_dict['oml:value'])
                elif 'oml:array_data' in evaluation_dict:
                    value = evaluation_dict['oml:array_data']
                else:
                    raise ValueError('Could not find keys "value" or '
                                     '"array_data" in %s' %
                                     str(evaluation_dict.keys()))
                if '@repeat' in evaluation_dict and '@fold' in \
                        evaluation_dict and '@sample' in evaluation_dict:
                    repeat = int(evaluation_dict['@repeat'])
                    fold = int(evaluation_dict['@fold'])
                    sample = int(evaluation_dict['@sample'])
                    if key not in sample_evaluations:
                        sample_evaluations[key] = OrderedDict()
                    if repeat not in sample_evaluations[key]:
                        sample_evaluations[key][repeat] = OrderedDict()
                    if fold not in sample_evaluations[key][repeat]:
                        sample_evaluations[key][repeat][fold] = OrderedDict()
                    sample_evaluations[key][repeat][fold][sample] = value
                elif '@repeat' in evaluation_dict and '@fold' in evaluation_dict:
                    repeat = int(evaluation_dict['@repeat'])
                    fold = int(evaluation_dict['@fold'])
                    if key not in fold_evaluations:
                        fold_evaluations[key] = OrderedDict()
                    if repeat not in fold_evaluations[key]:
                        fold_evaluations[key][repeat] = OrderedDict()
                    fold_evaluations[key][repeat][fold] = value
                else:
                    evaluations[key] = value

    if 'description' not in files and from_server is True:
        raise ValueError('No description file for run %d in run '
                         'description XML' % run_id)

    if 'predictions' not in files and from_server is True:
        task = openml.tasks.get_task(task_id)
        if task.task_type_id == TaskTypeEnum.SUBGROUP_DISCOVERY:
            raise NotImplementedError(
                'Subgroup discovery tasks are not yet supported.'
            )
        else:
            # JvR: actually, I am not sure whether this error should be raised.
            # a run can consist without predictions. But for now let's keep it
            # Matthias: yes, it should stay as long as we do not really handle
            # this stuff
            raise ValueError('No prediction files for run %d in run '
                             'description XML' % run_id)

    tags = openml.utils.extract_xml_tags('oml:tag', run)

    return OpenMLRun(run_id=run_id, uploader=uploader,
                     uploader_name=uploader_name, task_id=task_id,
                     task_type=task_type,
                     task_evaluation_measure=task_evaluation_measure,
                     flow_id=flow_id, flow_name=flow_name,
                     setup_id=setup_id, setup_string=setup_string,
                     parameter_settings=parameters,
                     dataset_id=dataset_id, output_files=files,
                     evaluations=evaluations,
                     fold_evaluations=fold_evaluations,
                     sample_evaluations=sample_evaluations,
                     tags=tags)


def _get_cached_run(run_id):
    """Load a run from the cache."""
    run_cache_dir = openml.utils._create_cache_directory_for_id(
        RUNS_CACHE_DIR_NAME, run_id,
    )
    try:
        run_file = os.path.join(run_cache_dir, "description.xml")
        with io.open(run_file, encoding='utf8') as fh:
            run = _create_run_from_xml(xml=fh.read())
        return run

    except (OSError, IOError):
        raise OpenMLCacheException("Run file for run id %d not "
                                   "cached" % run_id)


def list_runs(offset=None, size=None, id=None, task=None, setup=None,
              flow=None, uploader=None, tag=None, display_errors=False,
              **kwargs):
    """
    List all runs matching all of the given filters.
    (Supports large amount of results)

    Parameters
    ----------
    offset : int, optional
        the number of runs to skip, starting from the first
    size : int, optional
        the maximum number of runs to show

    id : list, optional

    task : list, optional

    setup: list, optional

    flow : list, optional

    uploader : list, optional

    tag : str, optional

    display_errors : bool, optional (default=None)
        Whether to list runs which have an error (for example a missing
        prediction file).

    kwargs : dict, optional
        Legal filter operators: task_type.

    Returns
    -------
    dict
        List of found runs.
    """

    if id is not None and (not isinstance(id, list)):
        raise TypeError('id must be of type list.')
    if task is not None and (not isinstance(task, list)):
        raise TypeError('task must be of type list.')
    if setup is not None and (not isinstance(setup, list)):
        raise TypeError('setup must be of type list.')
    if flow is not None and (not isinstance(flow, list)):
        raise TypeError('flow must be of type list.')
    if uploader is not None and (not isinstance(uploader, list)):
        raise TypeError('uploader must be of type list.')

    return openml.utils._list_all(
        _list_runs, offset=offset, size=size, id=id, task=task, setup=setup,
        flow=flow, uploader=uploader, tag=tag, display_errors=display_errors,
        **kwargs)


def _list_runs(id=None, task=None, setup=None,
               flow=None, uploader=None, display_errors=False, **kwargs):
    """
    Perform API call `/run/list/{filters}'
    <https://www.openml.org/api_docs/#!/run/get_run_list_filters>`

    Parameters
    ----------
    The arguments that are lists are separated from the single value
    ones which are put into the kwargs.
    display_errors is also separated from the kwargs since it has a
    default value.

    id : list, optional

    task : list, optional

    setup: list, optional

    flow : list, optional

    uploader : list, optional

    display_errors : bool, optional (default=None)
        Whether to list runs which have an error (for example a missing
        prediction file).

    kwargs : dict, optional
        Legal filter operators: task_type.

    Returns
    -------
    dict
        List of found runs.
    """

    api_call = "run/list"
    if kwargs is not None:
        for operator, value in kwargs.items():
            api_call += "/%s/%s" % (operator, value)
    if id is not None:
        api_call += "/run/%s" % ','.join([str(int(i)) for i in id])
    if task is not None:
        api_call += "/task/%s" % ','.join([str(int(i)) for i in task])
    if setup is not None:
        api_call += "/setup/%s" % ','.join([str(int(i)) for i in setup])
    if flow is not None:
        api_call += "/flow/%s" % ','.join([str(int(i)) for i in flow])
    if uploader is not None:
        api_call += "/uploader/%s" % ','.join([str(int(i)) for i in uploader])
    if display_errors:
        api_call += "/show_errors/true"
    return __list_runs(api_call)


def __list_runs(api_call):
    """Helper function to parse API calls which are lists of runs"""
    xml_string = openml._api_calls._perform_api_call(api_call, 'get')
    runs_dict = xmltodict.parse(xml_string, force_list=('oml:run',))
    # Minimalistic check if the XML is useful
    if 'oml:runs' not in runs_dict:
        raise ValueError('Error in return XML, does not contain "oml:runs": %s'
                         % str(runs_dict))
    elif '@xmlns:oml' not in runs_dict['oml:runs']:
        raise ValueError('Error in return XML, does not contain '
                         '"oml:runs"/@xmlns:oml: %s'
                         % str(runs_dict))
    elif runs_dict['oml:runs']['@xmlns:oml'] != 'http://openml.org/openml':
        raise ValueError('Error in return XML, value of  '
                         '"oml:runs"/@xmlns:oml is not '
                         '"http://openml.org/openml": %s'
                         % str(runs_dict))

    assert type(runs_dict['oml:runs']['oml:run']) == list, \
        type(runs_dict['oml:runs'])

    runs = OrderedDict()
    for run_ in runs_dict['oml:runs']['oml:run']:
        run_id = int(run_['oml:run_id'])
        run = {'run_id': run_id,
               'task_id': int(run_['oml:task_id']),
               'setup_id': int(run_['oml:setup_id']),
               'flow_id': int(run_['oml:flow_id']),
               'uploader': int(run_['oml:uploader']),
               'upload_time': str(run_['oml:upload_time']),
               'error_message': str((run_['oml:error_message']) or '')}

        runs[run_id] = run

    return runs
