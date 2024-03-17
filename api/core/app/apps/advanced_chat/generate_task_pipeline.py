import json
import logging
import time
from collections.abc import Generator
from typing import Any, Optional, Union, cast

from core.app.apps.base_app_queue_manager import AppQueueManager, PublishFrom
from core.app.entities.app_invoke_entities import (
    AdvancedChatAppGenerateEntity,
)
from core.app.entities.queue_entities import (
    QueueAdvancedChatMessageEndEvent,
    QueueAnnotationReplyEvent,
    QueueErrorEvent,
    QueueMessageReplaceEvent,
    QueueNodeFailedEvent,
    QueueNodeStartedEvent,
    QueueNodeSucceededEvent,
    QueuePingEvent,
    QueueRetrieverResourcesEvent,
    QueueStopEvent,
    QueueTextChunkEvent,
    QueueWorkflowFailedEvent,
    QueueWorkflowStartedEvent,
    QueueWorkflowSucceededEvent,
)
from core.app.entities.task_entities import (
    AdvancedChatTaskState,
    ChatbotAppBlockingResponse,
    ChatbotAppStreamResponse,
    MessageEndStreamResponse,
    StreamGenerateRoute,
)
from core.app.task_pipeline.based_generate_task_pipeline import BasedGenerateTaskPipeline
from core.app.task_pipeline.message_cycle_manage import MessageCycleManage
from core.app.task_pipeline.workflow_cycle_manage import WorkflowCycleManage
from core.file.file_obj import FileVar
from core.model_runtime.entities.llm_entities import LLMUsage
from core.workflow.entities.node_entities import NodeType, SystemVariable
from core.workflow.nodes.answer.answer_node import AnswerNode
from core.workflow.nodes.answer.entities import TextGenerateRouteChunk, VarGenerateRouteChunk
from events.message_event import message_was_created
from extensions.ext_database import db
from models.account import Account
from models.model import Conversation, EndUser, Message
from models.workflow import (
    Workflow,
    WorkflowNodeExecution,
    WorkflowRunStatus,
)

logger = logging.getLogger(__name__)


class AdvancedChatAppGenerateTaskPipeline(BasedGenerateTaskPipeline, WorkflowCycleManage, MessageCycleManage):
    """
    AdvancedChatAppGenerateTaskPipeline is a class that generate stream output and state management for Application.
    """
    _task_state: AdvancedChatTaskState
    _application_generate_entity: AdvancedChatAppGenerateEntity
    _workflow: Workflow
    _user: Union[Account, EndUser]
    _workflow_system_variables: dict[SystemVariable, Any]

    def __init__(self, application_generate_entity: AdvancedChatAppGenerateEntity,
                 workflow: Workflow,
                 queue_manager: AppQueueManager,
                 conversation: Conversation,
                 message: Message,
                 user: Union[Account, EndUser],
                 stream: bool) -> None:
        """
        Initialize AdvancedChatAppGenerateTaskPipeline.
        :param application_generate_entity: application generate entity
        :param workflow: workflow
        :param queue_manager: queue manager
        :param conversation: conversation
        :param message: message
        :param user: user
        :param stream: stream
        """
        super().__init__(application_generate_entity, queue_manager, user, stream)

        self._workflow = workflow
        self._conversation = conversation
        self._message = message
        self._workflow_system_variables = {
            SystemVariable.QUERY: message.query,
            SystemVariable.FILES: application_generate_entity.files,
            SystemVariable.CONVERSATION: conversation.id,
        }

        self._task_state = AdvancedChatTaskState(
            usage=LLMUsage.empty_usage()
        )

        if stream:
            self._stream_generate_routes = self._get_stream_generate_routes()
        else:
            self._stream_generate_routes = None

    def process(self) -> Union[ChatbotAppBlockingResponse, Generator[ChatbotAppStreamResponse, None, None]]:
        """
        Process generate task pipeline.
        :return:
        """
        db.session.refresh(self._workflow)
        db.session.refresh(self._user)
        db.session.close()

        if self._stream:
            generator = self._process_stream_response()
            for stream_response in generator:
                yield ChatbotAppStreamResponse(
                    conversation_id=self._conversation.id,
                    message_id=self._message.id,
                    created_at=int(self._message.created_at.timestamp()),
                    stream_response=stream_response
                )

            #     yield "data: " + json.dumps(response) + "\n\n"
        else:
            return self._process_blocking_response()

    def _process_blocking_response(self) -> ChatbotAppBlockingResponse:
        """
        Process blocking response.
        :return:
        """
        for queue_message in self._queue_manager.listen():
            event = queue_message.event

            if isinstance(event, QueueErrorEvent):
                err = self._handle_error(event)
                raise err
            elif isinstance(event, QueueRetrieverResourcesEvent):
                self._handle_retriever_resources(event)
            elif isinstance(event, QueueAnnotationReplyEvent):
                annotation = self._handle_annotation_reply(event)
                if annotation:
                    self._task_state.answer = annotation.content
            elif isinstance(event, QueueWorkflowStartedEvent):
                self._handle_workflow_start()
            elif isinstance(event, QueueNodeStartedEvent):
                self._handle_node_start(event)
            elif isinstance(event, QueueNodeSucceededEvent | QueueNodeFailedEvent):
                self._handle_node_finished(event)
            elif isinstance(event, QueueStopEvent | QueueWorkflowSucceededEvent | QueueWorkflowFailedEvent):
                workflow_run = self._handle_workflow_finished(event)

                if workflow_run.status != WorkflowRunStatus.SUCCEEDED.value:
                    raise self._handle_error(QueueErrorEvent(error=ValueError(f'Run failed: {workflow_run.error}')))

                # handle output moderation
                output_moderation_answer = self._handle_output_moderation_when_task_finished(self._task_state.answer)
                if output_moderation_answer:
                    self._task_state.answer = output_moderation_answer

                # Save message
                self._save_message()

                return self._to_blocking_response()
            elif isinstance(event, QueueTextChunkEvent):
                delta_text = event.text
                if delta_text is None:
                    continue

                if not self._is_stream_out_support(
                        event=event
                ):
                    continue

                # handle output moderation chunk
                should_direct_answer = self._handle_output_moderation_chunk(delta_text)
                if should_direct_answer:
                    continue

                self._task_state.answer += delta_text
            else:
                continue

        raise Exception('Queue listening stopped unexpectedly.')

    def _to_blocking_response(self) -> ChatbotAppBlockingResponse:
        """
        To blocking response.
        :return:
        """
        extras = {}
        if self._task_state.metadata:
            extras['metadata'] = self._task_state.metadata

        response = ChatbotAppBlockingResponse(
            task_id=self._application_generate_entity.task_id,
            data=ChatbotAppBlockingResponse.Data(
                id=self._message.id,
                mode=self._conversation.mode,
                conversation_id=self._conversation.id,
                message_id=self._message.id,
                answer=self._task_state.answer,
                created_at=int(self._message.created_at.timestamp()),
                **extras
            )
        )

        return response

    def _process_stream_response(self) -> Generator:
        """
        Process stream response.
        :return:
        """
        for message in self._queue_manager.listen():
            event = message.event

            if isinstance(event, QueueErrorEvent):
                err = self._handle_error(event)
                yield self._error_to_stream_response(err)
                break
            elif isinstance(event, QueueWorkflowStartedEvent):
                workflow_run = self._handle_workflow_start()
                yield self._workflow_start_to_stream_response(
                    task_id=self._application_generate_entity.task_id,
                    workflow_run=workflow_run
                )
            elif isinstance(event, QueueNodeStartedEvent):
                workflow_node_execution = self._handle_node_start(event)

                # search stream_generate_routes if node id is answer start at node
                if not self._task_state.current_stream_generate_state and event.node_id in self._stream_generate_routes:
                    self._task_state.current_stream_generate_state = self._stream_generate_routes[event.node_id]

                yield self._workflow_node_start_to_stream_response(
                    task_id=self._application_generate_entity.task_id,
                    workflow_node_execution=workflow_node_execution
                )
            elif isinstance(event, QueueNodeSucceededEvent | QueueNodeFailedEvent):
                workflow_node_execution = self._handle_node_finished(event)

                # stream outputs when node finished
                self._generate_stream_outputs_when_node_finished()

                yield self._workflow_node_finish_to_stream_response(
                    task_id=self._application_generate_entity.task_id,
                    workflow_node_execution=workflow_node_execution
                )
            elif isinstance(event, QueueStopEvent | QueueWorkflowSucceededEvent | QueueWorkflowFailedEvent):
                workflow_run = self._handle_workflow_finished(event)

                if workflow_run.status != WorkflowRunStatus.SUCCEEDED.value:
                    err_event = QueueErrorEvent(error=ValueError(f'Run failed: {workflow_run.error}'))
                    yield self._error_to_stream_response(self._handle_error(err_event))
                    break

                self._queue_manager.publish(
                    QueueAdvancedChatMessageEndEvent(),
                    PublishFrom.TASK_PIPELINE
                )

                yield self._workflow_finish_to_stream_response(
                    task_id=self._application_generate_entity.task_id,
                    workflow_run=workflow_run
                )
            elif isinstance(event, QueueAdvancedChatMessageEndEvent):
                output_moderation_answer = self._handle_output_moderation_when_task_finished(self._task_state.answer)
                if output_moderation_answer:
                    self._task_state.answer = output_moderation_answer
                    yield self._message_replace_to_stream_response(answer=output_moderation_answer)

                # Save message
                self._save_message()

                yield self._message_end_to_stream_response()
            elif isinstance(event, QueueRetrieverResourcesEvent):
                self._handle_retriever_resources(event)
            elif isinstance(event, QueueAnnotationReplyEvent):
                annotation = self._handle_annotation_reply(event)
                if annotation:
                    self._task_state.answer = annotation.content
            # elif isinstance(event, QueueMessageFileEvent):
            #     response = self._message_file_to_stream_response(event)
            #     if response:
            #         yield response
            elif isinstance(event, QueueTextChunkEvent):
                delta_text = event.text
                if delta_text is None:
                    continue

                if not self._is_stream_out_support(
                        event=event
                ):
                    continue

                # handle output moderation chunk
                should_direct_answer = self._handle_output_moderation_chunk(delta_text)
                if should_direct_answer:
                    continue

                self._task_state.answer += delta_text
                yield self._message_to_stream_response(delta_text, self._message.id)
            elif isinstance(event, QueueMessageReplaceEvent):
                yield self._message_replace_to_stream_response(answer=event.text)
            elif isinstance(event, QueuePingEvent):
                yield self._ping_stream_response()
            else:
                continue

    def _save_message(self) -> None:
        """
        Save message.
        :return:
        """
        self._message = db.session.query(Message).filter(Message.id == self._message.id).first()

        self._message.answer = self._task_state.answer
        self._message.provider_response_latency = time.perf_counter() - self._start_at
        self._message.workflow_run_id = self._task_state.workflow_run_id

        if self._task_state.metadata and self._task_state.metadata.get('usage'):
            usage = LLMUsage(**self._task_state.metadata['usage'])

            self._message.message_tokens = usage.prompt_tokens
            self._message.message_unit_price = usage.prompt_unit_price
            self._message.message_price_unit = usage.prompt_price_unit
            self._message.answer_tokens = usage.completion_tokens
            self._message.answer_unit_price = usage.completion_unit_price
            self._message.answer_price_unit = usage.completion_price_unit
            self._message.provider_response_latency = time.perf_counter() - self._start_at
            self._message.total_price = usage.total_price
            self._message.currency = usage.currency

        db.session.commit()

        message_was_created.send(
            self._message,
            application_generate_entity=self._application_generate_entity,
            conversation=self._conversation,
            is_first_message=self._application_generate_entity.conversation_id is None,
            extras=self._application_generate_entity.extras
        )

    def _message_end_to_stream_response(self) -> MessageEndStreamResponse:
        """
        Message end to stream response.
        :return:
        """
        extras = {}
        if self._task_state.metadata:
            extras['metadata'] = self._task_state.metadata

        return MessageEndStreamResponse(
            task_id=self._application_generate_entity.task_id,
            id=self._message.id,
            **extras
        )

    def _get_stream_generate_routes(self) -> dict[str, StreamGenerateRoute]:
        """
        Get stream generate routes.
        :return:
        """
        # find all answer nodes
        graph = self._workflow.graph_dict
        answer_node_configs = [
            node for node in graph['nodes']
            if node.get('data', {}).get('type') == NodeType.ANSWER.value
        ]

        # parse stream output node value selectors of answer nodes
        stream_generate_routes = {}
        for node_config in answer_node_configs:
            # get generate route for stream output
            answer_node_id = node_config['id']
            generate_route = AnswerNode.extract_generate_route_selectors(node_config)
            start_node_id = self._get_answer_start_at_node_id(graph, answer_node_id)
            if not start_node_id:
                continue

            stream_generate_routes[start_node_id] = StreamGenerateRoute(
                answer_node_id=answer_node_id,
                generate_route=generate_route
            )

        return stream_generate_routes

    def _get_answer_start_at_node_id(self, graph: dict, target_node_id: str) \
            -> Optional[str]:
        """
        Get answer start at node id.
        :param graph: graph
        :param target_node_id: target node ID
        :return:
        """
        nodes = graph.get('nodes')
        edges = graph.get('edges')

        # fetch all ingoing edges from source node
        ingoing_edge = None
        for edge in edges:
            if edge.get('target') == target_node_id:
                ingoing_edge = edge
                break

        if not ingoing_edge:
            return None

        source_node_id = ingoing_edge.get('source')
        source_node = next((node for node in nodes if node.get('id') == source_node_id), None)
        if not source_node:
            return None

        node_type = source_node.get('data', {}).get('type')
        if node_type in [
            NodeType.ANSWER.value,
            NodeType.IF_ELSE.value,
            NodeType.QUESTION_CLASSIFIER
        ]:
            start_node_id = target_node_id
        elif node_type == NodeType.START.value:
            start_node_id = source_node_id
        else:
            start_node_id = self._get_answer_start_at_node_id(graph, source_node_id)

        return start_node_id

    def _generate_stream_outputs_when_node_finished(self) -> None:
        """
        Generate stream outputs.
        :return:
        """
        if not self._task_state.current_stream_generate_state:
            return

        route_chunks = self._task_state.current_stream_generate_state.generate_route[
                       self._task_state.current_stream_generate_state.current_route_position:]

        for route_chunk in route_chunks:
            if route_chunk.type == 'text':
                route_chunk = cast(TextGenerateRouteChunk, route_chunk)
                for token in route_chunk.text:
                    self._queue_manager.publish(
                        QueueTextChunkEvent(
                            text=token
                        ), PublishFrom.TASK_PIPELINE
                    )
                    time.sleep(0.01)
            else:
                route_chunk = cast(VarGenerateRouteChunk, route_chunk)
                value_selector = route_chunk.value_selector
                route_chunk_node_id = value_selector[0]

                # check chunk node id is before current node id or equal to current node id
                if route_chunk_node_id not in self._task_state.ran_node_execution_infos:
                    break

                latest_node_execution_info = self._task_state.latest_node_execution_info

                # get route chunk node execution info
                route_chunk_node_execution_info = self._task_state.ran_node_execution_infos[route_chunk_node_id]
                if (route_chunk_node_execution_info.node_type == NodeType.LLM
                        and latest_node_execution_info.node_type == NodeType.LLM):
                    # only LLM support chunk stream output
                    self._task_state.current_stream_generate_state.current_route_position += 1
                    continue

                # get route chunk node execution
                route_chunk_node_execution = db.session.query(WorkflowNodeExecution).filter(
                    WorkflowNodeExecution.id == route_chunk_node_execution_info.workflow_node_execution_id).first()

                outputs = route_chunk_node_execution.outputs_dict

                # get value from outputs
                value = None
                for key in value_selector[1:]:
                    if not value:
                        value = outputs.get(key)
                    else:
                        value = value.get(key)

                if value:
                    text = None
                    if isinstance(value, str | int | float):
                        text = str(value)
                    elif isinstance(value, dict | list):
                        # handle files
                        file_vars = self._fetch_files_from_variable_value(value)
                        for file_var in file_vars:
                            try:
                                file_var_obj = FileVar(**file_var)
                            except Exception as e:
                                logger.error(f'Error creating file var: {e}')
                                continue

                            # convert file to markdown
                            text = file_var_obj.to_markdown()

                        if not text:
                            # other types
                            text = json.dumps(value, ensure_ascii=False)

                    if text:
                        for token in text:
                            self._queue_manager.publish(
                                QueueTextChunkEvent(
                                    text=token
                                ), PublishFrom.TASK_PIPELINE
                            )
                            time.sleep(0.01)

            self._task_state.current_stream_generate_state.current_route_position += 1

        # all route chunks are generated
        if self._task_state.current_stream_generate_state.current_route_position == len(
                self._task_state.current_stream_generate_state.generate_route):
            self._task_state.current_stream_generate_state = None

    def _is_stream_out_support(self, event: QueueTextChunkEvent) -> bool:
        """
        Is stream out support
        :param event: queue text chunk event
        :return:
        """
        if not event.metadata:
            return True

        if 'node_id' not in event.metadata:
            return True

        node_type = event.metadata.get('node_type')
        stream_output_value_selector = event.metadata.get('value_selector')
        if not stream_output_value_selector:
            return False

        if not self._task_state.current_stream_generate_state:
            return False

        route_chunk = self._task_state.current_stream_generate_state.generate_route[
            self._task_state.current_stream_generate_state.current_route_position]

        if route_chunk.type != 'var':
            return False

        if node_type != NodeType.LLM:
            # only LLM support chunk stream output
            return False

        route_chunk = cast(VarGenerateRouteChunk, route_chunk)
        value_selector = route_chunk.value_selector

        # check chunk node id is before current node id or equal to current node id
        if value_selector != stream_output_value_selector:
            return False

        return True

    def _handle_output_moderation_chunk(self, text: str) -> bool:
        """
        Handle output moderation chunk.
        :param text: text
        :return: True if output moderation should direct output, otherwise False
        """
        if self._output_moderation_handler:
            if self._output_moderation_handler.should_direct_output():
                # stop subscribe new token when output moderation should direct output
                self._task_state.answer = self._output_moderation_handler.get_final_output()
                self._queue_manager.publish(
                    QueueTextChunkEvent(
                        text=self._task_state.answer
                    ), PublishFrom.TASK_PIPELINE
                )

                self._queue_manager.publish(
                    QueueStopEvent(stopped_by=QueueStopEvent.StopBy.OUTPUT_MODERATION),
                    PublishFrom.TASK_PIPELINE
                )
                return True
            else:
                self._output_moderation_handler.append_new_token(text)

        return False