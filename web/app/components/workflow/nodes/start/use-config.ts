import { useCallback } from 'react'
import produce from 'immer'
import { useBoolean } from 'ahooks'
import type { StartNodeType } from './types'
import type { InputVar } from '@/app/components/workflow/types'
import useNodeCrud from '@/app/components/workflow/nodes/_base/hooks/use-node-crud'
import {
  useIsChatMode,
} from '@/app/components/workflow/hooks'

const useConfig = (id: string, payload: StartNodeType) => {
  const isChatMode = useIsChatMode()

  const { inputs, setInputs } = useNodeCrud<StartNodeType>(id, payload)

  const [isShowAddVarModal, {
    setTrue: showAddVarModal,
    setFalse: hideAddVarModal,
  }] = useBoolean(false)

  const handleVarListChange = useCallback((newList: InputVar[]) => {
    const newInputs = produce(inputs, (draft: any) => {
      draft.variables = newList
    })
    setInputs(newInputs)
  }, [inputs, setInputs])

  const handleAddVariable = useCallback((payload: InputVar) => {
    const newInputs = produce(inputs, (draft: StartNodeType) => {
      draft.variables.push(payload)
    })
    setInputs(newInputs)
  }, [inputs, setInputs])
  return {
    isChatMode,
    inputs,
    isShowAddVarModal,
    showAddVarModal,
    hideAddVarModal,
    handleVarListChange,
    handleAddVariable,
  }
}

export default useConfig
