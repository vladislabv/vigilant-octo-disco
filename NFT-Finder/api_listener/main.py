"""Here are defined all needed functions to start the fetching proccess to the defined database"""
import os
import asyncio
import nest_asyncio
import logging
import typing
import motor.motor_tornado
import tornado.ioloop
import tornado.web
from dotenv import load_dotenv
from pymongo.errors import BulkWriteError, DuplicateKeyError, WriteError
from async_api_caller import APICaller
from data_converter import PreparedItem
from schemas import schemas
from helpers.add_extensions import validate_item
logging.basicConfig(
    level=logging.INFO, 
    filename='api_listener.log', 
    format='%(asctime)s %(levelname)s:%(message)s'
    )

logger = logging.getLogger(__name__)

SECRETS_PATH = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)
        ), 
    "secrets", 
    ".env"
    )
load_dotenv(SECRETS_PATH)
"""
By design, asyncio does not allow its event loop to be nested and patches asyncio to allow nested use of 
asyncio.run and loop.run_until_complete.
"""
nest_asyncio.apply()

class MainHandler(tornado.web.RequestHandler):
    """Requests Handler provided by the Motor package: https://motor.readthedocs.io/en/stable/"""
    def get(self):
        db = self.settings['db']

async def get_server_info() -> None:
    """Function asserting that the database is right configurated

    Prints to the logs database information if the connection was successful
    """
    conn_str = os.getenv('MONGODB_CONNECTION_STRING')
    # set a 5-second connection timeout
    client = motor.motor_tornado.MotorClient(conn_str, serverSelectionTimeoutMS=5000)
    try:
        logger.info(f"Client Server info: {await client.server_info()}")
    except Exception:
        logger.error("Unable to connect to the server.")
    return client 

async def create_collections(db: tornado.web.Application, schemas: list[dict]):
    """Function managing asynction collections creation in the MongoDB (applied by first setup)

    Args:
        db (tornado.web.Application): Database object
        schemas (list[dict]): List of JSON schemas needed for the collection creation
    """
    try:
        logger.info(f"Collection created: {await db.create_collection(name = 'nft_item', validator = schemas['nft_item'])}")
        logger.info(f"Collection created: {await db.create_collection(name = 'item_attribute', validator = schemas['item_attribute'])}")
    except Exception:
        logger.exception("Unable to create collections.")

async def fetch_api_data(db: tornado.web.Application, do_insert: typing.Callable) -> None:
    """Function containing infinite loop, which is used for listenting to the Rarible API: https://api.rarible.org/v0.1

    Args:
        db (tornado.web.Application): Database object
        do_insert (typing.Callable): Function managing asyncronous inserting of supplied documents into the MongoDB
    """
    last_item_id = None
    last_activity_id = None

    while True:
        fetcher = APICaller(
            start_with_item = last_item_id,
            start_with_activity = last_activity_id
            )
        item_ids, last_activity_id = await fetcher.get_sell_activities()
        for item_id in item_ids:
            item = await fetcher.get_item_by_id(item_id)
            if validate_item(item):
                attrs, nft = PreparedItem(item).form_output()
                if len(attrs):
                    success = tornado.ioloop.IOLoop.current().run_sync( lambda: do_insert(db, 'nft_item', *[nft]) )
                    if success and len(attrs):
                        tornado.ioloop.IOLoop.current().run_sync( lambda: do_insert(db, 'item_attribute', *attrs) )
                await asyncio.sleep(0.1)
            else:
                continue

async def do_insert(db: tornado.web.Application, collection: str, *args: list[dict]) -> bool:
    """Function managing asyncronous inserting of supplied documents into the MongoDB

    Args:
        db (tornado.web.Application): Database object
        collection (str): Collection name

    Returns:
        bool: Whether the insertion was successfully completed
    """
    count = 0
    success = False
    for arg in args:
        try:
            await db[collection].insert_one(arg)
            count += 1
            success = True
        except (DuplicateKeyError, BulkWriteError, WriteError):
            pass
    if count > 0:        
        logger.info('Inserted %d documents' % (count,))
    return success

def make_app() -> tornado.web.Application:
    """Function defining a new asyncronous tornado application"""    
    return tornado.web.Application([(r"/", MainHandler),])

async def start_fetching(db: tornado.web.Application, fetch_api_data: typing.Callable, do_insert: typing.Callable, number_of_tasks: int) -> None:
    """Function creating fetch_api_data tasks, spawn maximum 10 processes in order not to overload the API with requests

    Args:
        db (tornado.web.Application): Database object
        fetch_api_data (typing.Callable): Function containing infinite loop, which is used for listenting to the Rarible API
        do_insert (typing.Callable): Function managing asyncronous inserting of supplied documents into the MongoDB
        number_of_tasks (int): number of parallel tasks to be spawned
    """ 
    tasks_list = await asyncio.gather(*[fetch_api_data(db, do_insert) for i in range(number_of_tasks)])
    logger.info(tasks_list)

if __name__ == "__main__":
    app = make_app()
    client = tornado.ioloop.IOLoop.current().run_sync(get_server_info)
    db = client.test_motor_database
    if str(os.getenv('FIRST_SETUP')) == 'TRUE':
        logger.info(f"Database created: {db}")
        tornado.ioloop.IOLoop.current().run_sync(lambda: create_collections(db, schemas))
    asyncio.run(start_fetching(db, fetch_api_data, do_insert, number_of_tasks = 10))