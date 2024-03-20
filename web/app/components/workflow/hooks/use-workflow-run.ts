import { useCallback } from 'react'
import {
  useReactFlow,
  useStoreApi,
} from 'reactflow'
import { useTranslation } from 'react-i18next'
import produce from 'immer'
import { useWorkflowStore } from '../store'
import {
  NodeRunningStatus,
  WorkflowRunningStatus,
} from '../types'
import { MAX_TREE_DEEPTH } from '../constants'
import { useNodesExtraData } from './use-nodes-data'
import { useWorkflow } from './use-workflow'
import { useStore as useAppStore } from '@/app/components/app/store'
import type { IOtherOptions } from '@/service/base'
import { ssePost } from '@/service/base'
import {
  fetchPublishedWorkflow,
  stopWorkflowRun,
} from '@/service/workflow'
import { useFeaturesStore } from '@/app/components/base/features/hooks'
import { useToastContext } from '@/app/components/base/toast'

export const useWorkflowRun = () => {
  const { t } = useTranslation()
  const { notify } = useToastContext()
  const store = useStoreApi()
  const workflowStore = useWorkflowStore()
  const reactflow = useReactFlow()
  const featuresStore = useFeaturesStore()
  const nodesExtraData = useNodesExtraData()
  const { getValidTreeNodes } = useWorkflow()

  const handleBackupDraft = useCallback(() => {
    const {
      getNodes,
      getEdges,
      getViewport,
    } = reactflow
    const {
      setBackupDraft,
    } = workflowStore.getState()

    setBackupDraft({
      nodes: getNodes(),
      edges: getEdges(),
      viewport: getViewport(),
    })
  }, [reactflow, workflowStore])

  const handleLoadBackupDraft = useCallback(() => {
    const {
      setNodes,
      setEdges,
    } = store.getState()
    const { setViewport } = reactflow
    const { backupDraft } = workflowStore.getState()

    if (backupDraft) {
      const {
        nodes,
        edges,
        viewport,
      } = backupDraft
      setNodes(nodes)
      setEdges(edges)
      setViewport(viewport)
    }
  }, [store, reactflow, workflowStore])

  const handleRunSetting = useCallback((shouldClear?: boolean) => {
    workflowStore.setState({ runningStatus: shouldClear ? undefined : WorkflowRunningStatus.Waiting })
    workflowStore.setState({ taskId: '' })
    workflowStore.setState({ currentSequenceNumber: 0 })
    workflowStore.setState({ workflowRunId: '' })
    const {
      setNodes,
      getNodes,
      edges,
      setEdges,
    } = store.getState()

    if (shouldClear) {
      handleLoadBackupDraft()
    }
    else {
      handleBackupDraft()
      const newNodes = produce(getNodes(), (draft) => {
        draft.forEach((node) => {
          node.data._runningStatus = NodeRunningStatus.Waiting
        })
      })
      setNodes(newNodes)
      const newEdges = produce(edges, (draft) => {
        draft.forEach((edge) => {
          edge.data._runned = false
        })
      })
      setEdges(newEdges)
    }
  }, [store, handleLoadBackupDraft, handleBackupDraft, workflowStore])

  const handleRun = useCallback((
    params: any,
    callback?: IOtherOptions,
  ) => {
    const {
      onWorkflowStarted,
      onWorkflowFinished,
      onNodeStarted,
      onNodeFinished,
      ...restCallback
    } = callback || {}
    const {
      getNodes,
      setNodes,
      edges,
      setEdges,
    } = store.getState()
    const appDetail = useAppStore.getState().appDetail
    const workflowContainer = document.getElementById('workflow-container')

    const {
      clientWidth,
      clientHeight,
    } = workflowContainer!

    let url = ''
    if (appDetail?.mode === 'advanced-chat')
      url = `/apps/${appDetail.id}/advanced-chat/workflows/draft/run`

    if (appDetail?.mode === 'workflow')
      url = `/apps/${appDetail.id}/workflows/draft/run`

    ssePost(
      url,
      {
        body: params,
      },
      {
        onWorkflowStarted: (params) => {
          const { task_id, workflow_run_id, data } = params
          workflowStore.setState({ runningStatus: WorkflowRunningStatus.Running })
          workflowStore.setState({ taskId: task_id })
          workflowStore.setState({ currentSequenceNumber: data.sequence_number })
          workflowStore.setState({ workflowRunId: workflow_run_id })
          const newNodes = produce(getNodes(), (draft) => {
            draft.forEach((node) => {
              node.data._runningStatus = NodeRunningStatus.Waiting
            })
          })
          setNodes(newNodes)

          if (onWorkflowStarted)
            onWorkflowStarted(params)
        },
        onWorkflowFinished: (params) => {
          const { data } = params
          workflowStore.setState({ runningStatus: data.status as WorkflowRunningStatus })

          if (onWorkflowFinished)
            onWorkflowFinished(params)
        },
        onNodeStarted: (params) => {
          const { data } = params
          const nodes = getNodes()
          const {
            setViewport,
          } = reactflow
          const currentNodeIndex = nodes.findIndex(node => node.id === data.node_id)
          const currentNode = nodes[currentNodeIndex]
          const position = currentNode.position
          const zoom = 1

          setViewport({
            x: (clientWidth - 400 - currentNode.width!) / 2 - position.x,
            y: (clientHeight - currentNode.height!) / 2 - position.y,
            zoom,
          })
          const newNodes = produce(nodes, (draft) => {
            draft[currentNodeIndex].data._runningStatus = NodeRunningStatus.Running
          })
          setNodes(newNodes)
          const newEdges = produce(edges, (draft) => {
            const edge = draft.find(edge => edge.target === data.node_id)

            if (edge)
              edge.data._runned = true
          })
          setEdges(newEdges)

          if (onNodeStarted)
            onNodeStarted(params)
        },
        onNodeFinished: (params) => {
          const { data } = params
          const newNodes = produce(getNodes(), (draft) => {
            const currentNode = draft.find(node => node.id === data.node_id)!

            currentNode.data._runningStatus = data.status
          })
          setNodes(newNodes)

          if (onNodeFinished)
            onNodeFinished(params)
        },
        ...restCallback,
      },
    )
  }, [store, reactflow, workflowStore])

  const handleStopRun = useCallback(() => {
    const appId = useAppStore.getState().appDetail?.id
    const taskId = workflowStore.getState().taskId

    stopWorkflowRun(`/apps/${appId}/workflow-runs/tasks/${taskId}/stop`)
  }, [workflowStore])

  const handleRestoreFromPublishedWorkflow = useCallback(async () => {
    const appDetail = useAppStore.getState().appDetail
    const publishedWorkflow = await fetchPublishedWorkflow(`/apps/${appDetail?.id}/workflows/publish`)

    if (publishedWorkflow) {
      const {
        setNodes,
        setEdges,
      } = store.getState()
      const { setViewport } = reactflow
      const nodes = publishedWorkflow.graph.nodes
      const edges = publishedWorkflow.graph.edges
      const viewport = publishedWorkflow.graph.viewport

      setNodes(nodes)
      setEdges(edges)
      if (viewport)
        setViewport(viewport)
      featuresStore?.setState({ features: publishedWorkflow.features })
      workflowStore.getState().setPublishedAt(publishedWorkflow.created_at)
    }
  }, [store, reactflow, featuresStore, workflowStore])

  const handleCheckBeforePublish = useCallback(() => {
    const {
      validNodes,
      maxDepth,
    } = getValidTreeNodes()

    if (!validNodes.length)
      return false

    if (maxDepth > MAX_TREE_DEEPTH) {
      notify({ type: 'error', message: t('workflow.common.maxTreeDepth', { depth: MAX_TREE_DEEPTH }) })
      return false
    }

    for (let i = 0; i < validNodes.length; i++) {
      const node = validNodes[i]
      const { errorMessage } = nodesExtraData[node.data.type].checkValid(node.data, t)

      if (errorMessage) {
        notify({ type: 'error', message: `[${node.data.title}] ${errorMessage}` })
        return false
      }
    }

    return true
  }, [getValidTreeNodes, nodesExtraData, notify, t])

  return {
    handleBackupDraft,
    handleRunSetting,
    handleRun,
    handleStopRun,
    handleRestoreFromPublishedWorkflow,
    handleCheckBeforePublish,
  }
}
