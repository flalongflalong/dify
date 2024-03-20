import type { CodeNodeType } from '../../../code/types'
import { BlockEnum, InputVarType, VarType } from '@/app/components/workflow/types'
import type { StartNodeType } from '@/app/components/workflow/nodes/start/types'
import type { NodeOutPutVar, ValueSelector, Var } from '@/app/components/workflow/types'
import type { VariableAssignerNodeType } from '@/app/components/workflow/nodes/variable-assigner/types'
import {
  CHAT_QUESTION_CLASSIFIER_OUTPUT_STRUCT,
  COMPLETION_QUESTION_CLASSIFIER_OUTPUT_STRUCT,
  HTTP_REQUEST_OUTPUT_STRUCT,
  KNOWLEDGE_RETRIEVAL_OUTPUT_STRUCT,
  LLM_OUTPUT_STRUCT,
  SUPPORT_OUTPUT_VARS_NODE,
  TEMPLATE_TRANSFORM_OUTPUT_STRUCT,
  TOOL_OUTPUT_STRUCT,
} from '@/app/components/workflow/constants'

const inputVarTypeToVarType = (type: InputVarType): VarType => {
  if (type === InputVarType.number)
    return VarType.number

  return VarType.string
}

const findExceptVarInObject = (obj: any, filterVar: (payload: Var, selector: ValueSelector) => boolean, value_selector: ValueSelector): Var => {
  const { children } = obj
  const res: Var = {
    variable: obj.variable,
    type: VarType.object,
    children: children.filter((item: Var) => {
      const { children } = item
      const currSelector = [...value_selector, item.variable]
      if (!children)
        return filterVar(item, currSelector)

      const obj = findExceptVarInObject(item, filterVar, currSelector)
      return obj.children && obj.children?.length > 0
    }),
  }
  return res
}

const formatItem = (item: any, isChatMode: boolean, filterVar: (payload: Var, selector: ValueSelector) => boolean): NodeOutPutVar => {
  const { id, data } = item
  const res: NodeOutPutVar = {
    nodeId: id,
    title: data.title,
    vars: [],
  }
  switch (data.type) {
    case BlockEnum.Start: {
      const {
        variables,
      } = data as StartNodeType
      res.vars = variables.map((v) => {
        return {
          variable: v.variable,
          type: inputVarTypeToVarType(v.type),
          isParagraph: v.type === InputVarType.paragraph,
          isSelect: v.type === InputVarType.select,
          options: v.options,
          required: v.required,
        }
      })
      if (isChatMode) {
        res.vars.push({
          variable: 'sys.query',
          type: VarType.string,
        })
      }
      res.vars.push({
        variable: 'sys.files',
        type: VarType.arrayFile,
      })
      break
    }

    case BlockEnum.LLM: {
      res.vars = LLM_OUTPUT_STRUCT
      break
    }

    case BlockEnum.KnowledgeRetrieval: {
      res.vars = KNOWLEDGE_RETRIEVAL_OUTPUT_STRUCT
      break
    }

    case BlockEnum.Code: {
      const {
        outputs,
      } = data as CodeNodeType
      res.vars = Object.keys(outputs).map((key) => {
        return {
          variable: key,
          type: outputs[key].type,
        }
      })
      break
    }

    case BlockEnum.TemplateTransform: {
      res.vars = TEMPLATE_TRANSFORM_OUTPUT_STRUCT
      break
    }

    case BlockEnum.QuestionClassifier: {
      res.vars = isChatMode ? CHAT_QUESTION_CLASSIFIER_OUTPUT_STRUCT : COMPLETION_QUESTION_CLASSIFIER_OUTPUT_STRUCT
      break
    }

    case BlockEnum.HttpRequest: {
      res.vars = HTTP_REQUEST_OUTPUT_STRUCT
      break
    }

    case BlockEnum.VariableAssigner: {
      const {
        output_type,
      } = data as VariableAssignerNodeType
      res.vars = [
        {
          variable: 'output',
          type: output_type,
        },
      ]
      break
    }

    case BlockEnum.Tool: {
      res.vars = TOOL_OUTPUT_STRUCT
      break
    }
  }

  const selector = [id]
  res.vars = res.vars.filter((v) => {
    const { children } = v
    if (!children)
      return filterVar(v, selector)

    const obj = findExceptVarInObject(v, filterVar, selector)
    return obj?.children && obj?.children.length > 0
  }).map((v) => {
    const { children } = v
    if (!children)
      return v

    return findExceptVarInObject(v, filterVar, selector)
  })

  return res
}
export const toNodeOutputVars = (nodes: any[], isChatMode: boolean, filterVar = (_payload: Var, _selector: ValueSelector) => true): NodeOutPutVar[] => {
  const res = nodes
    .filter(node => SUPPORT_OUTPUT_VARS_NODE.includes(node.data.type))
    .map((node) => {
      return {
        ...formatItem(node, isChatMode, filterVar),
        isStartNode: node.data.type === BlockEnum.Start,
      }
    })
    .filter(item => item.vars.length > 0)
  return res
}

export const isSystemVar = (valueSelector: ValueSelector) => {
  return valueSelector[0] === 'sys' || valueSelector[1] === 'sys'
}

export const getVarType = (value: ValueSelector, availableNodes: any[], isChatMode: boolean): VarType | undefined => {
  const isSystem = isSystemVar(value)
  const startNode = availableNodes.find((node: any) => {
    return node.data.type === BlockEnum.Start
  })
  const allOutputVars = toNodeOutputVars(availableNodes, isChatMode)

  const targetVarNodeId = isSystem ? startNode?.id : value[0]
  const targetVar = allOutputVars.find(v => v.nodeId === targetVarNodeId)

  if (!targetVar)
    return undefined

  let type: VarType = VarType.string
  let curr: any = targetVar.vars
  if (isSystem) {
    return curr.find((v: any) => v.variable === (value as ValueSelector).join('.'))?.type
  }
  else {
    (value as ValueSelector).slice(1).forEach((key, i) => {
      const isLast = i === value.length - 2
      curr = curr.find((v: any) => v.variable === key)
      if (isLast) {
        type = curr?.type
      }
      else {
        if (curr.type === VarType.object)
          curr = curr.children
      }
    })
    return type
  }
}
