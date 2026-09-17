from aiogram.fsm.state import State, StatesGroup

class SearchStates(StatesGroup):
    waiting_for_movie = State()
    waiting_for_music = State()
