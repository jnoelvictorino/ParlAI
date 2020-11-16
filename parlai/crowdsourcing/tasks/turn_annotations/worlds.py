#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import time
import os
import json
from typing import List, Optional

import numpy as np

from parlai.core.worlds import validate
from parlai.core.agents import create_agent_from_shared
from parlai.crowdsourcing.utils.acceptability import AcceptabilityChecker
from parlai.crowdsourcing.utils.worlds import CrowdOnboardWorld, CrowdTaskWorld
from parlai.crowdsourcing.tasks.turn_annotations.bot_agent import TurkLikeAgent

from parlai.crowdsourcing.tasks.turn_annotations.constants import (
    ONBOARD_CONFIG,
    ONBOARD_FAIL,
    ONBOARD_SUCCESS,
)

from parlai.crowdsourcing.tasks.turn_annotations.utils import (
    Compatibility,
    get_mturk_id_from_mephisto_wrapper,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mephisto.abstractions.blueprints.parlai_chat.parlai_chat_task_runner import (
        MephistoAgentWrapper,
    )


class TurnAnnotationsOnboardWorld(CrowdOnboardWorld):
    """
    This onboarding world displays a sample conversation with checkboxes of the same
    annotations as in the real HIT, but it is not a live conversation (all utterances
    are displayed at once).

    constants.py has the task data with correct answers in json form
    (opt['onboard_task_data']). User gets to try again onboard_failures_max_allowed
    times and is soft banned if they fail more than that.
    """

    def __init__(self, opt, agent: "MephistoAgentWrapper"):
        super().__init__(opt, agent)
        self.min_correct = ONBOARD_CONFIG['min_correct']
        self.max_incorrect = ONBOARD_CONFIG['max_incorrect']
        self.onboard_task_data = opt['onboard_task_data']
        self.status = 'DISCONNECT'
        self.onboard_statistics = opt['onboard_statistics']
        self.statistics_condition = opt['statistics_condition']
        self.max_onboard_time = opt['max_onboard_time']
        self.onboarding_qualification = opt['onboarding_qualification']
        self.worker_id = get_mturk_id_from_mephisto_wrapper(self.agent)

    def has_same_answer(self, ans1, ans2):
        if len(ans1) != len(ans2):
            return False

        ans1_sort = sorted(ans1)
        ans2_sort = sorted(ans2)

        for x in range(len(ans1_sort)):
            if ans1_sort[x] != ans2_sort[x]:
                return False
        return True

    def check_onboarding_answers(self, worker_answers):
        """
        Calculate how many correct answers the user gave.

        :param worker_answers: list of dicts containing mappings between an 
        annotation value and whether it was selected for each bucket.
        :return: boolean as to whether the worker passed or failed the task
        """
        given_turns = self.onboard_task_data['dialog']
        correct_answers = [t[1]['answers'] for t in given_turns]
        number_correct = 0
        number_incorrect = 0
        for worker_answer, correct_answer in zip(worker_answers, correct_answers):
            worker_only_selected = [
                key for key, selected in worker_answer.items() if selected
            ]
            if self.has_same_answer(worker_only_selected, correct_answer):
                number_correct += 1
            else:
                number_incorrect += 1

        print(
            f'Worker {self.worker_id} got {number_correct} annotations correct and {number_incorrect} incorrect in onboarding.'
        )
        if (
            number_correct >= self.min_correct
            and number_incorrect <= self.max_incorrect
        ):
            return True
        return False

    def parley(self):
        print(
            f'{self.__class__.__name__}: starting parley for worker_id: {self.worker_id}'
        )

        # We are rendering a frontend based on the initial task data, so we just
        # wait for the results to come in
        act = self.agent.act(timeout=self.max_onboard_time)
        self.status = self._handle_act(act)
        self.agent.observe({'id': 'SYSTEM', 'text': '', 'final_status': self.status})
        if self.status == ONBOARD_FAIL:
            start_time = time.time()
            # After soft ban, we just block in while loop until worker goes
            # away (Disconnects or returns the HIT as asked on the frontend)
            while time.time() - start_time < self.max_onboard_time:
                _ = self.agent.act(timeout=self.max_onboard_time)
                time.sleep(0.5)
        return None

    def _handle_act(self, act):
        if 'task_data' not in act:
            print(f'{self.__class__.__name__}: {self.worker_id} had no data submitted')
            return ONBOARD_FAIL

        worker_answers = act['task_data']['annotations']

        if self.check_onboarding_answers(worker_answers):
            print(f'Worker {self.worker_id} successfully passed the onboard task.')

            # This will end the onboarding and send them directly to the HIT
            self.episodeDone = True
            return ONBOARD_SUCCESS
        else:
            print(f'Worker {self.worker_id} failed onboarding.')
            # Grant the failed qualification, then sleep as we want worker to return
            self.agent.mephisto_agent.get_worker().grant_qualification(
                self.onboarding_qualification, 0
            )
            return ONBOARD_FAIL

    def shutdown(self):
        super().shutdown()
        with self.statistics_condition:
            if self.status not in self.onboard_statistics:
                self.onboard_statistics[self.status] = 0
            self.onboard_statistics[self.status] += 1


class TurnAnnotationsChatWorld(CrowdTaskWorld):
    def __init__(self, opt, agent=None, bot=None, context_info: Optional[dict] = None):
        super().__init__(opt, agent)

        # num_turns turns for a single side, and really it appears to be
        # (num_turns + 1) * 2 total b/c of the "Hi!" and first bot utterance

        num_turns = opt['num_turns']
        max_resp_time = opt['max_resp_time']

        self.opt = opt
        self.bot = bot
        self.task_turn_idx = 0
        self.num_turns = num_turns

        self.dialog = []
        self.tag = f'conversation_id {agent.mephisto_agent.db_id}'
        self.task_type = 'sandbox' if opt['is_sandbox'] else 'live'
        self.chat_done = False
        if context_info is not None:
            self.context_info = context_info
            self.personas = [
                self.context_info['persona_1_strings'],
                self.context_info['persona_2_strings'],
            ]
        else:
            self.context_info = {}
            self.personas = None
        self.check_acceptability = opt['check_acceptability']
        self.acceptability_checker = AcceptabilityChecker()
        self.block_qualification = opt['block_qualification']

        # below are timeout protocols
        self.max_resp_time = max_resp_time  # in secs
        print(
            f'Creating {self.__class__.__name__} for tag {self.tag} with {num_turns} turns.'
        )

    def __add_problem_data_to_utterance(self, p, turn_idx):
        # Human has just responded. Problem data received
        # now will be from bot's prior utterance (turn_idx
        # is also present to be safe that data matches)
        print(p)
        self.dialog[turn_idx]['problem_data'] = p

    def parley(self):
        print(
            f'{self.__class__.__name__}:{self.tag}: is at turn {self.task_turn_idx}, with {self.num_turns} pairs of turns needed...'
        )

        control_msg = {"episode_done": False}

        if self.task_turn_idx == 0:
            if self.opt['include_persona']:
                # The Bot agent
                # We add the personas and 1/3 of the time WoW topic as the
                # first utterance in the history.
                # Previously for BST task, we also had a big first utterance
                # that gave instructions. Removing that for this task.
                persona_strings = [s.strip() for s in self.personas[1]]
                persona_utterance = self._get_persona_utterance(
                    persona_strings=persona_strings,
                    context_dataset=self.context_info['context_dataset'],
                    additional_context=self.context_info['additional_context'],
                    is_bot=True,
                )
                message = control_msg.copy()
                message['text'] = persona_utterance
                # The bot seeing its persona does not count as a "turn"
                self.bot.observe(validate(message), increment_turn=False)

            if self.opt['conversation_start_mode'] == 'bst':
                print('[Displaying first utterances as per BST task.]')
                # Display the previous two utterances
                human_first_msg = {
                    'episode_done': False,
                    'id': self.agent.id,
                    'text': self.context_info['person1_seed_utterance'],
                    'fake_start': True,
                    'agent_idx': 0,
                }
                for k, v in control_msg.items():
                    human_first_msg[k] = v
                bot_first_msg = {
                    'episode_done': False,
                    'id': self.bot.id,
                    'text': self.context_info['person2_seed_utterance'],
                    'fake_start': True,
                    'agent_idx': 1,
                }
                print(
                    f'human_first_msg: {human_first_msg}, bot_first_msg: {bot_first_msg}'
                )

                self.dialog.append(human_first_msg)
                self.dialog.append(bot_first_msg)

                for observer in [self.agent, self.bot]:
                    observer.observe(validate(human_first_msg))
                    observer.observe(validate(bot_first_msg))

            elif self.opt['conversation_start_mode'] == 'hi':
                print('[Displaying "Hi!" only as per Meena task.]')
                human_first_msg = {
                    'episode_done': False,
                    'id': self.agent.id,
                    'text': 'Hi!',
                    'fake_start': True,
                    'agent_idx': 0,
                }
                for k, v in control_msg.items():
                    human_first_msg[k] = v

                self.dialog.append(human_first_msg)
                self.agent.observe(validate(human_first_msg))
                self.bot.observe(validate(human_first_msg))

                first_bot_act = self.bot.act()
                first_bot_act = Compatibility.maybe_fix_act(first_bot_act)

                self.agent.observe(validate(first_bot_act))

                bot_utterance_data = {
                    'agent_idx': 1,
                    'text': first_bot_act['text'],
                    'id': first_bot_act['id'],
                }
                self.dialog.append(bot_utterance_data)

            else:
                raise ValueError(
                    f"Conversation start mode {self.opt['conversation_start_mode']} "
                    f"not recognized!"
                )

            self.task_turn_idx += 1
            return

        """Otherwise, we proceed accordingly"""
        print(
            f'{self.__class__.__name__}:{self.tag}: About to act with task turn idx: {self.task_turn_idx}'
        )
        acts = [None, None]
        for idx, agent in enumerate([self.agent, self.bot]):
            if not self.chat_done:
                acts[idx] = agent.act(timeout=self.max_resp_time)
                acts[idx] = Compatibility.maybe_fix_act(acts[idx])
                print(
                    f'Got act for agent idx {idx}, act was: {acts[idx]} and self.task_turn_idx: {self.task_turn_idx}.'
                )

            if acts[idx].get('task_data', {}).get('final_rating') is not None:
                self.chat_done = True
                # agent ends chat after exceeding minimum number of turns
                if self.task_turn_idx > self.num_turns:
                    # Human has just responded. Problem data received
                    # now will be from bot's prior utterance (turn_idx
                    # is a also present to be safe that data matches)
                    p = acts[idx]['task_data']['problem_data_for_prior_message']
                    self.__add_problem_data_to_utterance(p, idx - 1)
                return

            else:
                utterance_data = {
                    'agent_idx': idx,
                    # Get rid of annotations HTML if it's the bot response
                    'text': acts[idx]['text'].split('<br>')[0],
                    'id': acts[idx]['id']
                    if 'id' in acts[idx]
                    else 'NULL_ID',  # Person1 or Polyencoder
                }
                self.dialog.append(utterance_data)
                if idx == 0:
                    # Human has just responded. Problem data received
                    # now will be from bot's prior utterance (turn_idx
                    # is a also present to be safe that data matches)
                    p = acts[idx]['task_data']['problem_data_for_prior_message']
                    self.__add_problem_data_to_utterance(p, idx - 1)

                for other_agent in [self.agent, self.bot]:
                    if other_agent != agent:
                        other_agent.observe(validate(acts[idx]))

                print(
                    f'[agent {idx}] self.task_turn_idx: {self.task_turn_idx}, self.dialog is: {self.dialog}'
                )
                self.task_turn_idx += 1

    def shutdown(self):

        if self.chat_done:
            self.opt['run_statistics'][self.bot.worker_id] += 1
            # {{{TODO: print run stats now}}}

        self.agent.shutdown()

    def episode_done(self):
        return self.chat_done

    def _get_persona_utterance(
        self,
        persona_strings: Optional[List[str]] = None,
        context_dataset: Optional[str] = None,
        additional_context: Optional[str] = None,
        is_bot: bool = False,
    ):
        if is_bot:
            # Pass back the original context
            persona_pieces = [f"your persona: {str_}" for str_ in persona_strings]
            if context_dataset == 'wizard_of_wikipedia':
                additional_context_pieces = [additional_context]
            else:
                additional_context_pieces = []
            full_context = '\n'.join(persona_pieces + additional_context_pieces)
            print(f'FULL CONTEXT: {full_context}')
            return full_context
        else:
            if context_dataset == 'convai2':
                last_sentence = 'Pretend that the conversation has already begun.'
            elif context_dataset == 'empathetic_dialogues':
                last_sentence = (
                    f'Pretend that the conversation has already begun, and that you '
                    f'had been talking about the following situation: '
                    f'<b>"{additional_context}"</b>'
                )
            elif context_dataset == 'wizard_of_wikipedia':
                last_sentence = (
                    f'Pretend that the conversation has already begun, and that you '
                    f'had been talking about <b>{additional_context}</b>.'
                )
            else:
                raise ValueError('Context dataset unrecognized!')
            joined_personas = '\n'.join(persona_strings)
            return (
                f'\nSuccessfully matched with another user! Now let\'s get to know '
                f'each other through the chat. You need to finish at least '
                f'<b>{self.num_turns} chat turns</b>, and after that you can click the '
                f'"Done" button to end the chat.\n\n'
                f'<b>Your character description is:\n<span style="color:blue">{joined_personas}</span></b> '
                '\n\n<b>Remember that you can get to know each '
                'other as your characters, talk about any topic, or talk about a '
                'situation that might have happened to your character.</b>'
                '\n<b>Do not trivially copy the '
                'character descriptions into the message.</b><br><br>'
                f'{last_sentence}'
            )

    def save_data(self):
        # TODO move to agent state

        if self.check_acceptability:
            human_texts = [
                message['text'] for message in self.dialog if message['agent_idx'] == 0
            ]
            violation_types = ['min_words', 'all_caps', 'exact_match', 'safety']
            if self.opt['conversation_start_mode'] == 'bst':
                # The BST mode starts the conversation with two previous utterances, so
                # there should be no new greeting
                violation_types.append('penalize_greetings')

            violations_string = self.acceptability_checker.check_messages(
                messages=human_texts, is_worker_0=False, violation_types=violation_types
            )
        else:
            violations_string = None

        time_string = time.strftime('%Y%m%d_%H%M%S')
        data_path = self.opt['save_folder']
        filename = os.path.join(
            data_path,
            '{}_{}_{}.json'.format(
                time_string, np.random.randint(0, 1000), self.task_type
            ),
        )
        with open(os.path.join(filename), 'w+') as f_json:
            data = {
                'personas': self.personas,
                'context_dataset': self.context_info.get('context_dataset'),
                'person1_seed_utterance': self.context_info.get(
                    'person1_seed_utterance'
                ),
                'person2_seed_utterance': self.context_info.get(
                    'person2_seed_utterance'
                ),
                'additional_context': self.context_info.get('additional_context'),
                'dialog': self.dialog,
                'workers': [get_mturk_id_from_mephisto_wrapper(self.agent)],
                'bad_workers': [],
                'acceptability_violations': (violations_string,),
                'hit_ids': [self.agent.mephisto_agent.task_run_id],
                'assignment_ids': [self.agent.mephisto_agent.assignment_id],
                'task_description': {
                    'annotations_config': self.opt['annotations_config'],
                    'model_nickname': self.bot.worker_id,
                    'model_file': self.bot.model_agent.opt.get('model_file'),
                    'model_opt': self.bot.model_agent.opt,
                },
            }
            # 'bad_workers' is for compatibility. Before, it was only non-empty if a
            # worker abandoned, returned, etc. a HIT, but now we don't even save chat
            # data in that case
            if self.check_acceptability:
                data['acceptability_violations'] = (violations_string,)
                # Make a tuple for compatibility with a human/human conversation in
                # which we check both sides for acceptability
            data_str = json.dumps(data)
            f_json.write(data_str)
        print(
            f'{self.__class__.__name__}:{self.tag}: Data successfully saved at '
            f'{filename} for model: {self.bot.worker_id}.'
        )
        if self.check_acceptability:
            print(f'Acceptability violations: {violations_string}')
            if violations_string != '':
                # Grant the failed qualification
                self.agent.mephisto_agent.get_worker().grant_qualification(
                    self.block_qualification, 1
                )


def make_onboarding_world(opt, agent):
    return TurnAnnotationsOnboardWorld(opt, agent)


def validate_onboarding(data):
    """Check the contents of the data to ensure they are valid"""
    print(f"Validating onboarding data {data}")
    messages = data['outputs']['messages']
    if len(messages) == 0:
        return False
    status_message = messages[-2]
    if status_message is None:
        return False
    submitted_data = status_message.get('data')
    if submitted_data is None:
        return False
    final_status = submitted_data.get('final_status')
    return final_status == ONBOARD_SUCCESS


def make_world(opt, agents):
    # Extract important components from opt
    semaphore = opt['semaphore']
    shared_bot_agents = opt['shared_bot_agents']
    statistics_condition = opt['statistics_condition']
    context_generator = opt['context_generator']
    num_turns = opt['num_turns']

    # Decide on a bot to use
    run_statistics = opt['run_statistics']
    with statistics_condition:
        remaining_counts_needed = [
            (m, c - run_statistics[m]) for (m, c) in opt['conversations_needed'].items()
        ]
        remaining_counts_needed.sort(reverse=True, key=lambda x: x[1])
        model_name = remaining_counts_needed[0][0]
        print(f'Remaining conversation counts needed: {remaining_counts_needed}')
        print(f'Choosing the "{model_name}" model for the bot.')
        run_statistics[model_name] += 1

    # Create the bot
    bot_agent = create_agent_from_shared(shared_bot_agents[model_name])
    bot_worker = TurkLikeAgent(
        opt,
        model_name=model_name,
        model_agent=bot_agent,
        num_turns=num_turns,
        semaphore=semaphore,
    )

    # Get context: personas, previous utterances, etc.
    if context_generator is not None:
        context_info = context_generator.get_context()
    else:
        context_info = None

    agents[0].agent_id = "Worker"

    return TurnAnnotationsChatWorld(
        opt, agent=agents[0], bot=bot_worker, context_info=context_info
    )


def get_world_params():
    return {"agent_count": 1}
