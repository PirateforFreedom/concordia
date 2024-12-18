# Copyright 2024 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A factory implementing the three key questions agent as an entity."""

from collections.abc import Callable, Sequence
import datetime
import json

from absl import logging as absl_logging
from concordia.agents import entity_agent_with_logging
from concordia.associative_memory import associative_memory
from concordia.associative_memory import formative_memories
from concordia.clocks import game_clock
from concordia.components import agent as agent_components
from concordia.components.agent import action_spec_ignored
from concordia.components.agent import memory_component
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.memory_bank import legacy_associative_memory
from concordia.typing import entity_component
from concordia.typing import logging
from concordia.typing import memory as memory_lib
from concordia.utils import measurements as measurements_lib
import numpy as np


DEFAULT_INSTRUCTIONS_COMPONENT_KEY = 'Instructions'
DEFAULT_INSTRUCTIONS_PRE_ACT_KEY = '\nInstructions'
DEFAULT_GOAL_COMPONENT_KEY = 'Goal'


def _get_class_name(object_: object) -> str:
  return object_.__class__.__name__


def _get_all_memories(
    memory_component_: agent_components.memory_component.MemoryComponent,
    add_time: bool = True,
    sort_by_time: bool = True,
    constant_score: float = 0.0,
) -> Sequence[memory_lib.MemoryResult]:
  """Returns all memories in the memory bank.

  Args:
    memory_component_: The memory component to retrieve memories from.
    add_time: whether to add time
    sort_by_time: whether to sort by time
    constant_score: assign this score value to each memory
  """
  texts = memory_component_.get_all_memories_as_text(add_time=add_time,
                                                     sort_by_time=sort_by_time)
  return [memory_lib.MemoryResult(text=t, score=constant_score) for t in texts]


def _get_earliest_timepoint(
    memory_component_: agent_components.memory_component.MemoryComponent,
) -> datetime.datetime:
  """Returns all memories in the memory bank.

  Args:
    memory_component_: The memory component to retrieve memories from.
  """
  memories_data_frame = memory_component_.get_raw_memory()
  if not memories_data_frame.empty:
    sorted_memories_data_frame = memories_data_frame.sort_values(
        'time', ascending=True)
    return sorted_memories_data_frame['time'][0]
  else:
    absl_logging.warn('No memories found in memory bank.')
    return datetime.datetime.now()


class SituationRepresentation(action_spec_ignored.ActionSpecIgnored):
  """Consider ``what kind of situation am I in now?``."""

  def __init__(
      self,
      model: language_model.LanguageModel,
      clock_now: Callable[[], datetime.datetime],
      memory_component_name: str = (
          memory_component.DEFAULT_MEMORY_COMPONENT_NAME
      ),
      pre_act_key: str = 'The current situation',
      logging_channel: logging.LoggingChannel = logging.NoOpLoggingChannel,
  ):
    """Initialize a component to consider the current situation.

    Args:
      model: The language model to use.
      clock_now: Function that returns the current time.
      memory_component_name: The name of the memory component from which to
        retrieve related memories.
      pre_act_key: Prefix to add to the output of the component when called
        in `pre_act`.
      logging_channel: The channel to log debug information to.
    """
    super().__init__(pre_act_key)
    self._model = model
    self._clock_now = clock_now
    self._memory_component_name = memory_component_name
    self._logging_channel = logging_channel

    self._previous_time = None
    self._situation_thus_far = None

  def _make_pre_act_value(self) -> str:
    """Returns a representation of the current situation to pre act."""
    agent_name = self.get_entity().name
    current_time = self._clock_now()
    memory = self.get_entity().get_component(
        self._memory_component_name,
        type_=memory_component.MemoryComponent)

    if self._situation_thus_far is None:
      self._previous_time = _get_earliest_timepoint(memory)
      chain_of_thought = interactive_document.InteractiveDocument(self._model)
      chain_of_thought.statement('~~ Creative Writing Assignment ~~')
      chain_of_thought.statement(f'Protagonist: {agent_name}')
      mems = '\n'.join([mem.text for mem in _get_all_memories(memory)])
      chain_of_thought.statement(f'Story fragments and world data:\n{mems}')
      chain_of_thought.statement(f'Events continue after {current_time}')
      self._situation_thus_far = chain_of_thought.open_question(
          question=(
              'Narratively summarize the story fragments and world data. Give '
              'special emphasis to atypical features of the setting such as '
              'when and where the story takes place as well as any causal '
              'mechanisms or affordances mentioned in the information '
              'provided. Highlight the goals, personalities, occupations, '
              'skills, and affordances of the named characters and '
              'relationships between them. Use third-person omniscient '
              'perspective.'),
          max_tokens=1000,
          terminators=(),
          question_label='Exercise')

    interval_scorer = legacy_associative_memory.RetrieveTimeInterval(
        time_from=self._previous_time,
        time_until=current_time,
        add_time=True,
    )
    mems = [mem.text for mem in memory.retrieve(scoring_fn=interval_scorer)]
    result = '\n'.join(mems) + '\n'
    chain_of_thought = interactive_document.InteractiveDocument(self._model)
    chain_of_thought.statement(f'Context:\n{self._situation_thus_far}')
    chain_of_thought.statement(f'Protagonist: {agent_name}')
    chain_of_thought.statement(
        f'Thoughts and memories of {agent_name}:\n{result}'
    )
    self._situation_thus_far = chain_of_thought.open_question(
        question=(
            'What situation does the protagonist find themselves in? '
            'Make sure to provide enough detail to give the '
            'reader a comprehensive understanding of the world '
            'inhabited by the protagonist, their affordances in that '
            'world, actions they may be able to take, effects their '
            'actions may produce, and what is currently going on.'
        ),
        max_tokens=1000,
        terminators=(),
        question_label='Exercise',
    )
    chain_of_thought.statement(f'The current date and time is {current_time}')

    self._logging_channel({
        'Key': self.get_pre_act_key(),
        'Value': self._situation_thus_far,
        'Chain of thought': chain_of_thought.view().text().splitlines(),
    })

    self._previous_time = current_time

    return self._situation_thus_far

  def get_state(self) -> entity_component.ComponentState:
    """Converts the component to JSON data."""
    with self._lock:
      if self._previous_time is None:
        previous_time = ''
      else:
        previous_time = self._previous_time.strftime('%Y-%m-%d %H:%M:%S')
      return {
          'previous_time': previous_time,
          'situation_thus_far': self._situation_thus_far,
      }

  def set_state(self, state: entity_component.ComponentState) -> None:
    """Sets the component state from JSON data."""
    with self._lock:
      if state['previous_time']:
        previous_time = datetime.datetime.strptime(
            state['previous_time'], '%Y-%m-%d %H:%M:%S')
      else:
        previous_time = None
      self._previous_time = previous_time
      self._situation_thus_far = state['situation_thus_far']


def build_agent(
    *,
    config: formative_memories.AgentConfig,
    model: language_model.LanguageModel,
    memory: associative_memory.AssociativeMemory,
    clock: game_clock.MultiIntervalClock,
    update_time_interval: datetime.timedelta | None = None,
) -> entity_agent_with_logging.EntityAgentWithLogging:
  """Build an agent.

  Args:
    config: The agent config to use.
    model: The language model to use.
    memory: The agent's memory object.
    clock: The clock to use.
    update_time_interval: Agent calls update every time this interval passes.

  Returns:
    An agent.
  """
  del update_time_interval
  if not config.extras.get('main_character', False):
    raise ValueError('This function is meant for a main character '
                     'but it was called on a supporting character.')

  agent_name = config.name

  raw_memory = legacy_associative_memory.AssociativeMemoryBank(memory)

  measurements = measurements_lib.Measurements()
  instructions = agent_components.instructions.Instructions(
      agent_name=agent_name,
      pre_act_key=DEFAULT_INSTRUCTIONS_PRE_ACT_KEY,
      logging_channel=measurements.get_channel('Instructions').on_next,
  )

  time_display = agent_components.report_function.ReportFunction(
      function=clock.current_time_interval_str,
      pre_act_key='\nCurrent time',
      logging_channel=measurements.get_channel('TimeDisplay').on_next,
  )

  observation_label = '\nObservation'
  observation = agent_components.observation.Observation(
      clock_now=clock.now,
      timeframe=clock.get_step_size(),
      pre_act_key=observation_label,
      logging_channel=measurements.get_channel('Observation').on_next,
  )

  situation_representation_label = (
      f'\nQuestion: What situation is {agent_name} in right now?\nAnswer')
  situation_representation = (
      SituationRepresentation(
          model=model,
          clock_now=clock.now,
          pre_act_key=situation_representation_label,
          logging_channel=measurements.get_channel(
              'SituationRepresentation'
          ).on_next,
      )
  )
  self_perception_label = (
      f'\nQuestion: What kind of person is {agent_name}?\nAnswer')
  self_perception = agent_components.question_of_recent_memories.SelfPerception(
      model=model,
      pre_act_key=self_perception_label,
      logging_channel=measurements.get_channel('SelfPerception').on_next,
  )

  person_by_situation_label = (
      f'\nQuestion: What would a person like {agent_name} do in '
      'a situation like this?\nAnswer')
  person_by_situation = (
      agent_components.question_of_recent_memories.PersonBySituation(
          model=model,
          components={
              _get_class_name(self_perception): self_perception_label,
              _get_class_name(
                  situation_representation): situation_representation_label,
          },
          clock_now=clock.now,
          pre_act_key=person_by_situation_label,
          logging_channel=measurements.get_channel('PersonBySituation').on_next,
      )
  )
  relevant_memories_label = '\nRecalled memories and observations'
  relevant_memories = agent_components.all_similar_memories.AllSimilarMemories(
      model=model,
      components={
          _get_class_name(
              situation_representation): situation_representation_label,
          _get_class_name(time_display): 'The current date/time is'},
      num_memories_to_retrieve=10,
      pre_act_key=relevant_memories_label,
      logging_channel=measurements.get_channel('AllSimilarMemories').on_next,
  )

  if config.goal:
    goal_label = '\nGoal'
    overarching_goal = agent_components.constant.Constant(
        state=config.goal,
        pre_act_key=goal_label,
        logging_channel=measurements.get_channel(goal_label).on_next)
  else:
    overarching_goal = None

  entity_components = (
      # Components that provide pre_act context.
      time_display,
      observation,
      self_perception,
      situation_representation,
      person_by_situation,
      relevant_memories,
  )
  components_of_agent = {_get_class_name(component): component
                         for component in entity_components}
  components_of_agent[
      agent_components.memory_component.DEFAULT_MEMORY_COMPONENT_NAME] = (
          agent_components.memory_component.MemoryComponent(raw_memory))
  component_order = list(components_of_agent.keys())

  # Put the instructions first.
  components_of_agent[DEFAULT_INSTRUCTIONS_COMPONENT_KEY] = instructions
  component_order.insert(0, DEFAULT_INSTRUCTIONS_COMPONENT_KEY)
  if overarching_goal is not None:
    components_of_agent[DEFAULT_GOAL_COMPONENT_KEY] = overarching_goal
    # Place goal after the instructions.
    component_order.insert(1, DEFAULT_GOAL_COMPONENT_KEY)

  act_component = agent_components.concat_act_component.ConcatActComponent(
      model=model,
      clock=clock,
      component_order=component_order,
      logging_channel=measurements.get_channel('ActComponent').on_next,
  )

  agent = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name=agent_name,
      act_component=act_component,
      context_components=components_of_agent,
      component_logging=measurements,
  )

  return agent


def save_to_json(
    agent: entity_agent_with_logging.EntityAgentWithLogging,
) -> str:
  """Saves an agent to JSON data.

  This function saves the agent's state to a JSON string, which can be loaded
  afterwards with `rebuild_from_json`. The JSON data
  includes the state of the agent's context components, act component, memory,
  agent name and the initial config. The clock, model and embedder are not
  saved and will have to be provided when the agent is rebuilt. The agent must
  be in the `READY` phase to be saved.

  Args:
    agent: The agent to save.

  Returns:
    A JSON string representing the agent's state.

  Raises:
    ValueError: If the agent is not in the READY phase.
  """

  if agent.get_phase() != entity_component.Phase.READY:
    raise ValueError('The agent must be in the `READY` phase to be saved.')

  data = {
      component_name: agent.get_component(component_name).get_state()
      for component_name in agent.get_all_context_components()
  }

  data['act_component'] = agent.get_act_component().get_state()

  config = agent.get_config()
  if config is not None:
    data['agent_config'] = config.to_dict()

  return json.dumps(data)


def rebuild_from_json(
    json_data: str,
    model: language_model.LanguageModel,
    clock: game_clock.MultiIntervalClock,
    embedder: Callable[[str], np.ndarray],
    memory_importance: Callable[[str], float] | None = None,
) -> entity_agent_with_logging.EntityAgentWithLogging:
  """Rebuilds an agent from JSON data."""

  data = json.loads(json_data)

  new_agent_memory = associative_memory.AssociativeMemory(
      sentence_embedder=embedder,
      importance=memory_importance,
      clock=clock.now,
      clock_step_size=clock.get_step_size(),
  )

  if 'agent_config' not in data:
    raise ValueError('The JSON data does not contain the agent config.')
  agent_config = formative_memories.AgentConfig.from_dict(
      data.pop('agent_config')
  )

  agent = build_agent(
      config=agent_config,
      model=model,
      memory=new_agent_memory,
      clock=clock,
  )

  for component_name in agent.get_all_context_components():
    agent.get_component(component_name).set_state(data.pop(component_name))

  agent.get_act_component().set_state(data.pop('act_component'))

  assert not data, f'Unused data {sorted(data)}'
  return agent
