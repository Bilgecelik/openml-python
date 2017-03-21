import collections
import hashlib
import re
import time
import unittest

import xmltodict

import scipy.stats
import sklearn
import sklearn.datasets
import sklearn.decomposition
import sklearn.dummy
import sklearn.ensemble
import sklearn.feature_selection
import sklearn.model_selection
import sklearn.pipeline
import sklearn.preprocessing
import sklearn.tree

from openml.testing import TestBase
from openml._api_calls import _perform_api_call
import openml
from openml.flows.sklearn_converter import _format_external_version


def are_flows_equal(flow1, flow2):
    """Check equality of two flows.

    Two flows are equal if their all keys which are not set by the server
    are equal, as well as all their parameters and components.
    """
    if not isinstance(flow2, flow1.__class__):
        return False

    # Name is actually not generated by the server, but it will be
    # tested further down with a getter (allows mocking in the tests)
    generated_by_the_server = ['name', 'flow_id', 'uploader', 'version',
                               'upload_date', 'source_url',
                               'binary_url', 'source_format',
                               'binary_format', 'source_md5',
                               'binary_md5', 'model']

    for key in set(flow1.__dict__.keys()).union(flow2.__dict__.keys()):
        if key in generated_by_the_server:
            continue
        attr1 = getattr(flow1, key, None)
        attr2 = getattr(flow2, key, None)
        if key == 'components':
            for name in set(attr1.keys()).union(attr2.keys()):
                if not are_flows_equal(attr1[name], attr2[name]):
                    return False
        else:
            if attr1 != attr2:
                return False
    return True


def get_sentinel():
    # Create a unique prefix for the flow. Necessary because the flow is
    # identified by its name and external version online. Having a unique
    #  name allows us to publish the same flow in each test run
    md5 = hashlib.md5()
    md5.update(str(time.time()).encode('utf-8'))
    sentinel = md5.hexdigest()[:10]
    sentinel = 'TEST%s' % sentinel
    return sentinel


class TestFlow(TestBase):

    def test_get_flow(self):
        # We need to use the production server here because 4024 is not the test
        # server
        openml.config.server = self.production_server

        flow = openml.flows.get_flow(4024)
        self.assertIsInstance(flow, openml.OpenMLFlow)
        self.assertEqual(flow.flow_id, 4024)
        self.assertEqual(len(flow.parameters), 24)
        self.assertEqual(len(flow.components), 1)

        subflow_1 = list(flow.components.values())[0]
        self.assertIsInstance(subflow_1, openml.OpenMLFlow)
        self.assertEqual(subflow_1.flow_id, 4025)
        self.assertEqual(len(subflow_1.parameters), 14)
        self.assertEqual(subflow_1.parameters['E'], 'CC')
        self.assertEqual(len(subflow_1.components), 1)

        subflow_2 = list(subflow_1.components.values())[0]
        self.assertIsInstance(subflow_2, openml.OpenMLFlow)
        self.assertEqual(subflow_2.flow_id, 4026)
        self.assertEqual(len(subflow_2.parameters), 13)
        self.assertEqual(subflow_2.parameters['I'], '10')
        self.assertEqual(len(subflow_2.components), 1)

        subflow_3 = list(subflow_2.components.values())[0]
        self.assertIsInstance(subflow_3, openml.OpenMLFlow)
        self.assertEqual(subflow_3.flow_id, 1724)
        self.assertEqual(len(subflow_3.parameters), 11)
        self.assertEqual(subflow_3.parameters['L'], '-1')
        self.assertEqual(len(subflow_3.components), 0)

    def test_from_xml_to_xml(self):
        # Get the raw xml thing
        # TODO maybe get this via get_flow(), which would have to be refactored to allow getting only the xml dictionary
        for flow_id in [1185, 1244, 1196, 1112, ]:
            flow_xml = _perform_api_call("flow/%d" % flow_id)
            flow_dict = xmltodict.parse(flow_xml)

            flow = openml.OpenMLFlow._from_dict(flow_dict)
            new_xml = flow._to_xml()

            flow_xml = flow_xml.replace('  ', '').replace('\t', '').strip().replace('\n\n', '\n').replace('&quot;', '"')
            flow_xml = re.sub(r'^$', '', flow_xml)
            new_xml = new_xml.replace('  ', '').replace('\t', '').strip().replace('\n\n', '\n').replace('&quot;', '"')
            new_xml = re.sub(r'^$', '', new_xml)

            self.assertEqual(new_xml, flow_xml)

    def test_to_xml_from_xml(self):
        scaler = sklearn.preprocessing.StandardScaler(with_mean=False)
        boosting = sklearn.ensemble.AdaBoostClassifier(
            base_estimator=sklearn.tree.DecisionTreeClassifier())
        model = sklearn.pipeline.Pipeline(steps=(
            ('scaler', scaler), ('boosting', boosting)))
        flow = openml.flows.sklearn_to_flow(model)
        flow.flow_id = -234
        # end of setup

        xml = flow._to_xml()
        xml_dict = xmltodict.parse(xml)
        new_flow = openml.flows.OpenMLFlow._from_dict(xml_dict)
        self.assertTrue(are_flows_equal(new_flow, flow))
        self.assertIsNot(new_flow, flow)

    def test_publish_flow(self):
        sentinel = get_sentinel()

        flow = openml.OpenMLFlow(name='Test',
                                 description="test description",
                                 model=sklearn.dummy.DummyClassifier(),
                                 components=collections.OrderedDict(),
                                 parameters=collections.OrderedDict(),
                                 parameters_meta_info=collections.OrderedDict(),
                                 external_version=_format_external_version(
                                     'sklearn', sklearn.__version__),
                                 tags=[],
                                 language='English',
                                 dependencies='')
        flow.name = 'TEST%s%s' % (sentinel, flow.name)

        flow.publish()
        self.assertIsInstance(flow.flow_id, int)

    def test_ensure_flow_exists(self):
        sentinel = get_sentinel()

        flow = openml.OpenMLFlow(name='Test',
                                 description="test description",
                                 model=sklearn.dummy.DummyClassifier(),
                                 components=collections.OrderedDict(),
                                 parameters=collections.OrderedDict(),
                                 parameters_meta_info=collections.OrderedDict(),
                                 external_version=_format_external_version(
                                     'sklearn', sklearn.__version__),
                                 tags=[],
                                 language='English',
                                 dependencies='')
        flow.name = 'TEST%s%s' % (sentinel, flow.name)
        flow_id = flow._ensure_flow_exists()
        self.assertIsInstance(flow_id, int)
        self.assertEqual(flow._ensure_flow_exists(), flow_id)

    def test_sklearn_to_upload_to_flow(self):
        iris = sklearn.datasets.load_iris()
        X = iris.data
        y = iris.target
        sentinel = get_sentinel()

        # Test a more complicated flow
        ohe = sklearn.preprocessing.OneHotEncoder(categorical_features=[1])
        scaler = sklearn.preprocessing.StandardScaler(with_mean=False)
        pca = sklearn.decomposition.TruncatedSVD()
        fs = sklearn.feature_selection.SelectPercentile(
            score_func=sklearn.feature_selection.f_classif, percentile=30)
        fu = sklearn.pipeline.FeatureUnion(transformer_list=[
            ('pca', pca), ('fs', fs)])
        boosting = sklearn.ensemble.AdaBoostClassifier(
            base_estimator=sklearn.tree.DecisionTreeClassifier())
        model = sklearn.pipeline.Pipeline(steps=[('ohe', ohe), ('scaler', scaler),
                                                 ('fu', fu), ('boosting', boosting)])
        parameter_grid = {'boosting__n_estimators': [1, 5, 10, 100],
                          'boosting__learning_rate': scipy.stats.uniform(0.01, 0.99),
                          'boosting__base_estimator__max_depth': scipy.stats.randint(1, 10)}
        cv = sklearn.model_selection.StratifiedKFold(n_splits=5, shuffle=True)
        rs = sklearn.model_selection.RandomizedSearchCV(
            estimator=model, param_distributions=parameter_grid, cv=cv)
        rs.fit(X, y)
        flow = openml.flows.sklearn_to_flow(rs)
        flow.tags.extend(['openml-python', 'unittest'])

        # Add the sentinel to all name strings in all subflows. Adds it to
        # name to make it easier in the web gui to see that the flow is only
        # a test flow
        to_visit = collections.deque()
        to_visit.appendleft(flow)
        while len(to_visit) > 0:
            current_flow = to_visit.pop()
            for sub_flow in current_flow.components.values():
                to_visit.appendleft(sub_flow)
            current_flow.name = sentinel + current_flow.name

        flow.publish()
        self.assertIsInstance(flow.flow_id, int)

        # Check whether we can load the flow again
        # Remove the sentinel from the name again so that we can reinstantiate
        # the object again
        new_flow = openml.flows.get_flow(flow_id=flow.flow_id)

        local_xml = flow._to_xml()
        server_xml = new_flow._to_xml()

        local_xml = re.sub('<oml:id>[0-9]+</oml:id>', '', local_xml)
        server_xml = re.sub('<oml:id>[0-9]+</oml:id>', '', server_xml)
        server_xml = re.sub('<oml:uploader>[0-9]+</oml:uploader>', '', server_xml)
        server_xml = re.sub('<oml:version>[0-9]+</oml:version>', '', server_xml)
        server_xml = re.sub('<oml:upload_date>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}</oml:upload_date>', '', server_xml)

        for i in range(10):
            # Make sure that we replace all occurences of two newlines
            local_xml = local_xml.replace(sentinel, '')
            local_xml = local_xml.replace('  ', '').replace('\t', '').strip().replace('\n\n', '\n').replace('&quot;', '"')
            local_xml = re.sub(r'(^$)', '', local_xml)
            server_xml = server_xml.replace(sentinel, '')
            server_xml = server_xml.replace('  ', '').replace('\t', '').strip().replace('\n\n', '\n').replace('&quot;', '"')
            server_xml = re.sub(r'^$', '', server_xml)

        self.assertEqual(server_xml, local_xml)

        self.assertTrue(are_flows_equal(new_flow, flow))
        self.assertIsNot(new_flow, flow)

        fixture_name = '%ssklearn.model_selection._search.RandomizedSearchCV(' \
                       'estimator=sklearn.pipeline.Pipeline(' \
                       'ohe=sklearn.preprocessing.data.OneHotEncoder,' \
                       'scaler=sklearn.preprocessing.data.StandardScaler,' \
                       'fu=sklearn.pipeline.FeatureUnion(' \
                       'pca=sklearn.decomposition.truncated_svd.TruncatedSVD,' \
                       'fs=sklearn.feature_selection.univariate_selection.SelectPercentile),' \
                       'boosting=sklearn.ensemble.weight_boosting.AdaBoostClassifier(' \
                       'base_estimator=sklearn.tree.tree.DecisionTreeClassifier)))' \
                        % sentinel

        self.assertEqual(new_flow.name, fixture_name)
        self.assertTrue('openml-python' in new_flow.tags)
        self.assertTrue('unittest' in new_flow.tags)
        new_flow.model.fit(X, y)
