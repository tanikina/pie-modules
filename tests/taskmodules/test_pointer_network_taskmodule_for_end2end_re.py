import logging
from dataclasses import asdict, dataclass
from typing import Dict, List, Set

import pytest
import torch
from pytorch_ie.annotations import BinaryRelation, LabeledSpan
from pytorch_ie.core import AnnotationList, Document, annotation_field
from pytorch_ie.documents import TextBasedDocument

from pie_modules.taskmodules import PointerNetworkTaskModuleForEnd2EndRE
from pie_modules.taskmodules.common.metrics import AnnotationLayerMetric
from pie_modules.taskmodules.pointer_network_taskmodule_for_end2end_re import (
    LabelsAndOptionalConstraints,
    get_first_occurrence_index,
)

logger = logging.getLogger(__name__)

DUMP_FIXTURE_DATA = False


def _config_to_str(cfg: Dict[str, str]) -> str:
    result = "-".join([f"{k}={cfg[k]}" for k in sorted(cfg)])
    return result


CONFIGS = [{}, {"partition_layer_name": "sentences"}]
CONFIG_DICT = {_config_to_str(cfg): cfg for cfg in CONFIGS}


@pytest.fixture(scope="module", params=CONFIG_DICT.keys())
def config_str(request):
    return request.param


@pytest.fixture(scope="module")
def config(config_str):
    return CONFIG_DICT[config_str]


@pytest.fixture(scope="module")
def document():
    @dataclass
    class ExampleDocument(TextBasedDocument):
        entities: AnnotationList[LabeledSpan] = annotation_field(target="text")
        relations: AnnotationList[BinaryRelation] = annotation_field(target="entities")
        sentences: AnnotationList[LabeledSpan] = annotation_field(target="text")

    doc = ExampleDocument(text="This is a dummy text about nothing. Trust me.")
    span1 = LabeledSpan(start=10, end=20, label="content")
    span2 = LabeledSpan(start=27, end=34, label="topic")
    span3 = LabeledSpan(start=42, end=44, label="person")
    doc.entities.extend([span1, span2, span3])
    assert str(span1) == "dummy text"
    assert str(span2) == "nothing"
    assert str(span3) == "me"
    rel = BinaryRelation(head=span1, tail=span2, label="is_about")
    doc.relations.append(rel)
    assert str(rel.label) == "is_about"
    assert str(rel.head) == "dummy text"
    assert str(rel.tail) == "nothing"

    no_rel = BinaryRelation(head=span1, tail=span3, label="no_relation")
    doc.relations.append(no_rel)
    assert str(no_rel.label) == "no_relation"
    assert str(no_rel.head) == "dummy text"
    assert str(no_rel.tail) == "me"

    sent1 = LabeledSpan(start=0, end=35, label="1")
    sent2 = LabeledSpan(start=36, end=45, label="2")
    doc.sentences.extend([sent1, sent2])
    assert str(sent1) == "This is a dummy text about nothing."
    assert str(sent2) == "Trust me."
    return doc


def test_document(document):
    spans = document.entities
    assert len(spans) == 3
    assert (str(spans[0]), spans[0].label) == ("dummy text", "content")
    assert (str(spans[1]), spans[1].label) == ("nothing", "topic")
    assert (str(spans[2]), spans[2].label) == ("me", "person")
    relations = document.relations
    assert len(relations) == 2
    assert (str(relations[0].head), relations[0].label, str(relations[0].tail)) == (
        "dummy text",
        "is_about",
        "nothing",
    )
    assert (str(relations[1].head), relations[1].label, str(relations[1].tail)) == (
        "dummy text",
        "no_relation",
        "me",
    )
    sentences = document.sentences
    assert len(sentences) == 2
    assert str(sentences[0]) == "This is a dummy text about nothing."
    assert str(sentences[1]) == "Trust me."


@pytest.fixture(scope="module")
def taskmodule(document, config):
    taskmodule = PointerNetworkTaskModuleForEnd2EndRE(
        span_layer_name="entities",
        relation_layer_name="relations",
        exclude_labels_per_layer={"relations": ["no_relation"]},
        annotation_field_mapping={
            "entities": "labeled_spans",
            "relations": "binary_relations",
        },
        create_constraints=True,
        tokenizer_kwargs={"strict_span_conversion": False},
        **config,
    )

    taskmodule.prepare(documents=[document])
    return taskmodule


def test_taskmodule(taskmodule):
    assert taskmodule.is_prepared
    assert taskmodule.prepared_attributes == {
        "labels_per_layer": {
            "entities": ["content", "person", "topic"],
            "relations": ["is_about"],
        },
    }
    assert taskmodule.layer_names == ["entities", "relations"]
    assert taskmodule.special_targets == ["<s>", "</s>"]
    assert taskmodule.labels == ["none", "content", "person", "topic", "is_about"]
    assert taskmodule.targets == [
        "<s>",
        "</s>",
        "none",
        "content",
        "person",
        "topic",
        "is_about",
    ]
    assert taskmodule.bos_id == 0
    assert taskmodule.eos_id == 1
    assert taskmodule.none_id == 2
    assert taskmodule.span_ids == [3, 4, 5]
    assert taskmodule.relation_ids == [6]
    assert taskmodule.label2id == {
        "content": 3,
        "is_about": 6,
        "none": 2,
        "person": 4,
        "topic": 5,
    }
    assert taskmodule.label_embedding_weight_mapping == {
        50265: [45260],
        50266: [39763],
        50267: [354, 1215, 9006],
        50268: [5970],
        50269: [10166],
    }
    assert taskmodule.target_tokens == [
        "<s>",
        "</s>",
        "<<none>>",
        "<<content>>",
        "<<person>>",
        "<<topic>>",
        "<<is_about>>",
    ]
    assert taskmodule.target_token_ids == [0, 2, 50266, 50269, 50268, 50265, 50267]


def test_prepared_config(taskmodule, config):
    if config == {}:
        assert taskmodule._config() == {
            "taskmodule_type": "PointerNetworkTaskModuleForEnd2EndRE",
            "span_layer_name": "entities",
            "relation_layer_name": "relations",
            "none_label": "none",
            "loop_dummy_relation_name": "loop",
            "labels_per_layer": {
                "entities": ["content", "person", "topic"],
                "relations": ["is_about"],
            },
            "exclude_labels_per_layer": {"relations": ["no_relation"]},
            "create_constraints": True,
            "document_type": "pytorch_ie.documents.TextDocumentWithLabeledSpansBinaryRelationsAndLabeledPartitions",
            "tokenized_document_type": "pie_modules.documents.TokenDocumentWithLabeledSpansBinaryRelationsAndLabeledPartitions",
            "tokenizer_name_or_path": "facebook/bart-base",
            "tokenizer_init_kwargs": None,
            "tokenizer_kwargs": {"strict_span_conversion": False},
            "partition_layer_name": None,
            "annotation_field_mapping": {
                "entities": "labeled_spans",
                "relations": "binary_relations",
            },
            "label_tokens": None,
            "label_representations": None,
            "log_first_n_examples": None,
        }
    elif config == {"partition_layer_name": "sentences"}:
        assert taskmodule._config() == {
            "taskmodule_type": "PointerNetworkTaskModuleForEnd2EndRE",
            "span_layer_name": "entities",
            "relation_layer_name": "relations",
            "none_label": "none",
            "loop_dummy_relation_name": "loop",
            "labels_per_layer": {
                "entities": ["content", "person", "topic"],
                "relations": ["is_about"],
            },
            "exclude_labels_per_layer": {"relations": ["no_relation"]},
            "create_constraints": True,
            "document_type": "pytorch_ie.documents.TextDocumentWithLabeledSpansBinaryRelationsAndLabeledPartitions",
            "tokenized_document_type": "pie_modules.documents.TokenDocumentWithLabeledSpansBinaryRelationsAndLabeledPartitions",
            "tokenizer_name_or_path": "facebook/bart-base",
            "tokenizer_init_kwargs": None,
            "tokenizer_kwargs": {"strict_span_conversion": False},
            "partition_layer_name": "sentences",
            "annotation_field_mapping": {
                "entities": "labeled_spans",
                "relations": "binary_relations",
            },
            "label_tokens": None,
            "label_representations": None,
            "log_first_n_examples": None,
        }
    else:
        raise Exception(f"unknown config: {config}")


@pytest.fixture()
def task_encoding_without_target(taskmodule, document):
    return taskmodule.encode_input(document)[0]


def test_input_encoding(task_encoding_without_target, taskmodule):
    assert task_encoding_without_target is not None
    tokens = taskmodule.tokenizer.convert_ids_to_tokens(
        task_encoding_without_target.inputs.input_ids
    )
    if taskmodule.partition_layer_name is None:
        assert asdict(task_encoding_without_target.inputs) == {
            "input_ids": [0, 713, 16, 10, 34759, 2788, 59, 1085, 4, 3101, 162, 4, 2],
            "attention_mask": [1] * 13,
        }
    elif taskmodule.partition_layer_name == "sentences":
        assert asdict(task_encoding_without_target.inputs) == {
            "input_ids": [0, 713, 16, 10, 34759, 2788, 59, 1085, 4, 2],
            "attention_mask": [1] * 10,
        }
    else:
        raise Exception(f"unknown partition_layer_name: {taskmodule.partition_layer_name}")


@pytest.fixture()
def target_encoding(taskmodule, task_encoding_without_target):
    return taskmodule.encode_target(task_encoding_without_target)


def test_target_encoding(target_encoding, taskmodule):
    assert target_encoding is not None
    if taskmodule.partition_layer_name is None:
        assert asdict(target_encoding) == {
            "labels": [14, 14, 5, 11, 12, 3, 6, 17, 17, 4, 2, 2, 2, 2, 1],
            "constraints": [
                [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            ],
        }
    elif taskmodule.partition_layer_name == "sentences":
        assert asdict(target_encoding) == {
            "labels": [14, 14, 5, 11, 12, 3, 6, 1],
            "constraints": [
                [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0],
                [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            ],
        }
    else:
        raise Exception(f"unknown partition_layer_name: {taskmodule.partition_layer_name}")


@pytest.fixture()
def task_encoding(task_encoding_without_target, target_encoding):
    task_encoding_without_target.targets = target_encoding
    return task_encoding_without_target


def _separate_constraint(constraint, taskmodule):
    special_ids = sorted(taskmodule.special_target2id.values())
    none_ids = [taskmodule.none_id]
    span_ids = taskmodule.span_ids
    rel_ids = taskmodule.relation_ids
    result = [[constraint[id] for id in ids] for ids in [special_ids, none_ids, span_ids, rel_ids]]
    result += [constraint[taskmodule.pointer_offset :]]
    assert sum(len(con_part) for con_part in result) == len(constraint)
    return result


def test_build_constraints(taskmodule, task_encoding, config):
    input_len = len(task_encoding.inputs.input_ids)
    target_ids = task_encoding.targets.labels
    max_id = input_len + taskmodule.pointer_offset
    if config == {}:
        assert input_len == 13
        assert target_ids == [14, 14, 5, 11, 12, 3, 6, 17, 17, 4, 2, 2, 2, 2, 1]
        assert len(target_ids) == 15
        constraints = taskmodule.build_constraints(input_len, target_ids)
        constraints_tensor = torch.tensor(constraints)
        assert max_id == 20
        assert constraints_tensor.shape == (len(target_ids), max_id)
        constraints_formatted = [_separate_constraint(c, taskmodule) for c in constraints]
        assert constraints_formatted == [
            # [bos, eos], [none], [content, person, topic], [is_about] [offsets (all remaining)]
            [[0, 0], [0], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [1], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [1, 1, 1], [1], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [1], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 1], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
        ]
    elif config == {"partition_layer_name": "sentences"}:
        assert input_len == 10
        assert target_ids == [14, 14, 5, 11, 12, 3, 6, 1]
        assert len(target_ids) == 8
        constraints = taskmodule.build_constraints(input_len, target_ids)
        constraints_tensor = torch.tensor(constraints)
        assert max_id == 17
        assert constraints_tensor.shape == (len(target_ids), max_id)
        constraints_formatted = [_separate_constraint(c, taskmodule) for c in constraints]
        assert constraints_formatted == [
            # [bos, eos], [none], [content, person, topic], [is_about] [offsets (all remaining)]
            [[0, 0], [0], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 1, 1, 1]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [0], [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
            [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 1, 1, 1, 0, 0, 0]],
            [[0, 0], [1], [1, 1, 1], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 0], [1], [0, 0, 0], [1], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
            [[0, 1], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]],
        ]
    else:
        raise Exception(f"unknown config: {config}")


def test_build_constraints_single_label(taskmodule):
    input_len = 13
    target_ids = [14]
    max_id = input_len + taskmodule.pointer_offset
    constraints = taskmodule.build_constraints(input_len, target_ids)
    constraints_tensor = torch.tensor(constraints)
    assert constraints_tensor.shape == (len(target_ids), max_id)
    constraints_formatted = [_separate_constraint(c, taskmodule) for c in constraints]
    assert constraints_formatted == [
        [[0, 0], [1], [0, 0, 0], [0], [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0]]
    ]


# def test_build_constraints_empty(taskmodule):
#    input_len = 13
#    target_ids = []
#    max_id = input_len + taskmodule.pointer_offset
#    constraints = taskmodule.build_constraints(input_len, target_ids)
#    constraints_tensor = torch.tensor(constraints)
#    assert constraints_tensor.shape == (len(target_ids), max_id)
#    assert constraints == []


def test_maybe_log_example(taskmodule, task_encoding, caplog, config):
    original_log_first_n_examples = taskmodule.log_first_n_examples
    taskmodule.log_first_n_examples = 1
    caplog.clear()
    with caplog.at_level(logging.INFO):
        taskmodule.maybe_log_example(task_encoding)
    if config == {}:
        assert caplog.messages == [
            "*** Example ***",
            "doc.id:       None-tokenized-1-of-1",
            "input_ids:    0 713 16 10 34759 2788 59 1085 4 3101 162 4 2",
            "input_tokens: <s> This Ġis Ġa Ġdummy Ġtext Ġabout Ġnothing . ĠTrust Ġme . " "</s>",
            "label_ids:    14 14 5 11 12 3 6 17 17 4 2 2 2 2 1",
            "label_tokens: 14 {Ġnothing} 14 {Ġnothing} topic 11 {Ġdummy} 12 {Ġtext} content is_about 17 {Ġme} 17 {Ġme} person none none none none </s>",
            "constraints:  Shape(15, 20) (content is omitted)",
        ]
    elif config == {"partition_layer_name": "sentences"}:
        assert caplog.messages == [
            "*** Example ***",
            "doc.id:       None-tokenized-1-of-2",
            "input_ids:    0 713 16 10 34759 2788 59 1085 4 2",
            "input_tokens: <s> This Ġis Ġa Ġdummy Ġtext Ġabout Ġnothing . </s>",
            "label_ids:    14 14 5 11 12 3 6 1",
            "label_tokens: 14 {Ġnothing} 14 {Ġnothing} topic 11 {Ġdummy} 12 {Ġtext} content is_about </s>",
            "constraints:  Shape(8, 17) (content is omitted)",
        ]
    else:
        raise Exception(f"unknown config: {config}")

    # restore original value
    taskmodule.log_first_n_examples = original_log_first_n_examples


def test_maybe_log_example_disabled(taskmodule, task_encoding, caplog):
    original_log_first_n_examples = taskmodule.log_first_n_examples
    taskmodule.log_first_n_examples = None
    caplog.clear()
    with caplog.at_level(logging.INFO):
        taskmodule.maybe_log_example(task_encoding)
    assert caplog.record_tuples == []

    # restore original value
    taskmodule.log_first_n_examples = original_log_first_n_examples


@pytest.fixture()
def task_encodings(taskmodule, document):
    return taskmodule.encode(documents=[document], encode_target=True)


@pytest.fixture()
def batch(taskmodule, task_encodings):
    return taskmodule.collate(task_encodings)


def test_collate(batch, taskmodule):
    inputs, targets = batch
    for tensor in inputs.values():
        assert isinstance(tensor, torch.Tensor)
        assert tensor.dtype == torch.int64
    for tensor in targets.values():
        assert isinstance(tensor, torch.Tensor)
        assert tensor.dtype == torch.int64
    inputs_lists = {k: inputs[k].tolist() for k in sorted(inputs)}
    targets_lists = {k: targets[k].tolist() for k in sorted(targets)}
    if taskmodule.partition_layer_name is None:
        assert inputs_lists == {
            "input_ids": [[0, 713, 16, 10, 34759, 2788, 59, 1085, 4, 3101, 162, 4, 2]],
            "attention_mask": [[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
        }
        assert targets_lists == {
            "constraints": [
                [
                    [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                ]
            ],
            "labels": [[14, 14, 5, 11, 12, 3, 6, 17, 17, 4, 2, 2, 2, 2, 1]],
            "decoder_attention_mask": [[1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]],
        }
    elif taskmodule.partition_layer_name == "sentences":
        assert inputs_lists == {
            "input_ids": [
                [0, 713, 16, 10, 34759, 2788, 59, 1085, 4, 2],
                [0, 18823, 162, 4, 2, 1, 1, 1, 1, 1],
            ],
            "attention_mask": [[1, 1, 1, 1, 1, 1, 1, 1, 1, 1], [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]],
        }
        assert targets_lists == {
            "constraints": [
                [
                    [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                ],
                [
                    [0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1],
                    [0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 1, 1, -1, -1, -1, -1, -1],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, -1, -1, -1, -1, -1],
                    [0, 0, 1, 0, 0, 0, 0, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1],
                    [0, 0, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, -1, -1, -1, -1, -1],
                    [0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, -1, -1, -1, -1, -1],
                    [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, -1, -1, -1, -1, -1],
                    [0, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, -1, -1, -1, -1],
                ],
            ],
            "labels": [[14, 14, 5, 11, 12, 3, 6, 1], [9, 9, 4, 2, 2, 2, 2, 1]],
            "decoder_attention_mask": [
                [1, 1, 1, 1, 1, 1, 1, 1],
                [1, 1, 1, 1, 1, 1, 1, 1],
            ],
        }
    else:
        raise Exception(f"unknown partition_layer_name: {taskmodule.partition_layer_name}")


@pytest.fixture()
def unbatched_output(taskmodule, batch):
    inputs, targets = batch
    # because the model is trained to reproduce the target tokens, we can just use them as model prediction
    return taskmodule.unbatch_output(targets["labels"])


@pytest.fixture()
def task_outputs(unbatched_output):
    return unbatched_output


@pytest.fixture()
def task_output(task_outputs) -> LabelsAndOptionalConstraints:
    return task_outputs[0]


def test_task_output(task_output, taskmodule):
    output_list = task_output.labels
    if taskmodule.partition_layer_name is None:
        assert output_list == [14, 14, 5, 11, 12, 3, 6, 17, 17, 4, 2, 2, 2, 2, 1]
    elif taskmodule.partition_layer_name == "sentences":
        assert output_list == [14, 14, 5, 11, 12, 3, 6, 1]
    else:
        raise Exception(f"unknown partition_layer_name: {taskmodule.partition_layer_name}")


def _test_annotations_from_output(task_encodings, task_outputs, taskmodule, layer_names_expected):
    assert len(task_outputs) == len(task_encodings)

    # this needs to be outside the below loop because documents can contain duplicates
    # which would break the comparison when clearing predictions that were already added
    for task_encoding in task_encodings:
        for layer_name in layer_names_expected:
            task_encoding.document[layer_name].predictions.clear()

    layer_names: Set[str] = set()
    # Note: this list may contain duplicates!
    documents: List[Document] = []
    for i in range(len(task_outputs)):
        task_encoding = task_encodings[i]
        task_output = task_outputs[i]
        documents.append(task_encoding.document)

        for layer_name, annotation in taskmodule.create_annotations_from_output(
            task_encoding=task_encoding, task_output=task_output
        ):
            task_encoding.document[layer_name].predictions.append(annotation)
            layer_names.add(layer_name)

    assert layer_names == layer_names_expected

    for document in documents:
        for layer_name in layer_names:
            layer = {
                str(ann)
                for ann in document[layer_name].predictions
                if ann.label in taskmodule.labels_per_layer[layer_name]
            }
            layer_expected = {
                str(ann)
                for ann in document[layer_name]
                if ann.label in taskmodule.labels_per_layer[layer_name]
            }
            assert layer == layer_expected

    # this needs to be outside the above loop because documents can contain duplicates
    # which would break the comparison when clearing predictions too early
    for document in documents:
        for layer_name in layer_names:
            document[layer_name].predictions.clear()


def test_annotations_from_output(task_encodings, task_outputs, taskmodule):
    _test_annotations_from_output(
        taskmodule=taskmodule,
        task_encodings=task_encodings,
        task_outputs=task_outputs,
        layer_names_expected={"entities", "relations"},
    )


def test_configure_model_metric(taskmodule):
    metric = taskmodule.configure_model_metric()
    assert metric is not None
    assert isinstance(metric, AnnotationLayerMetric)


def test_configure_model_generation(taskmodule):
    assert taskmodule.configure_model_generation() == {
        "no_repeat_ngram_size": 7,
    }


def test_get_first_occurrence_index():
    tensor = torch.tensor(
        [
            [0, 1, 1, 1, 1, 1],
            [0, 0, 1, 1, 1, 1],
            [0, 1, 1, 0, 0, 1],
            [1, 1, 1, 1, 1, 1],
            [0, 0, 0, 0, 0, 0],
        ]
    )
    indices = get_first_occurrence_index(tensor, 1)
    torch.testing.assert_close(indices, torch.tensor([1, 2, 1, 0, 6]))
