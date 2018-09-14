import ibapi.client
import ibapi.wrapper
import threading
import queue
import datetime

DEFAULT_HISTORIC_DATA_ID = 50
DEFAULT_GET_CONTRACT_ID = 43

## marker for when queue is finished
FINISHED = object()
STARTED = object()
TIME_OUT = object()

class finishableQueue(object):
    def __init__(self, queue_to_finish):
        self._queue, self.status = queue_to_finish, STARTED

    def get(self, timeout):
        """
        Returns a list of queue elements once timeout is finished, or a FINISHED flag is received in the queue
        :param timeout: how long to wait before giving up
        :return: list of queue elements
        """
        contents_of_queue = []
        finished = False

        while not finished:
            try:
                current_element = self._queue.get(timeout = timeout)
                if current_element is FINISHED:
                    finished = True
                    self.status = FINISHED
                else:
                    contents_of_queue.append(current_element)
                    ## keep going and try and get more data

            except queue.Empty:
                ## If we hit a time out it's most probable we're not getting a finished element any time soon
                ## give up and return what we have
                finished = True
                self.status = TIME_OUT
        return (contents_of_queue)

    def timed_out(self):
        return self.status is TIME_OUT

class ibApiWrapper(ibapi.wrapper.EWrapper):
    # The wrapper deals with the action coming back from the IB gateway or TWS instance
    def __init__(self):
        self._contractDetailsDict, self._historicDataDict = {}, {}
        self.initError()

    # error handling code
    def initError(self):
        self._errorQueue = queue.Queue()

    def getError(self, timeout = 5):
        if self.isError():
            try:
                return self._errorQueue.get(timeout = timeout)
            except queue.Empty:
                return None
        return None

    def isError(self):
        return not self._errorQueue.empty()

    def error(self, id, errorCode, errorString):  # Overriding
        self._errorQueue.put("ibApiWrapper|errorID:%d|errorCode:%d|%s" % (id, errorCode, errorString))

    def ContractDetailsInit(self, reqId):
        contractDetailsQueue = self._contractDetailsDict[reqId] = queue.Queue()
        return contractDetailsQueue

    def contractDetails(self, reqId, contractDetails):  # Overriding
        if reqId not in self._contractDetailsDict: self.ContractDetailsInit(reqId)
        self._contractDetailsDict[reqId].put(contractDetails)

    def contractDetailsEnd(self, reqId):  # Overriding
        if reqId not in self._contractDetailsDict: self.ContractDetailsInit(reqId)
        self._contractDetailsDict[reqId].put(FINISHED)

    # Historic Data
    def historicalDataInit(self, reqId):
        historic_data_queue = self._historicDataDict[reqId] = queue.Queue()
        return historic_data_queue

    def historicalData(self, reqId, bar):  # Overriding
        bardata = (bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume, bar.barCount, bar.average)
        if reqId not in self._historicDataDict: self.historicalDataInit(reqId)
        self._historicDataDict[reqId].put(bardata)

    def historicalDataEnd(self, reqId, start: str, end: str):  # Overriding
        if reqId not in self._historicDataDict: self.historicalDataInit(reqId)
        self._historicDataDict[reqId].put(FINISHED)

class TestClient(ibapi.client.EClient):
    def __init__(self, wrapper):
        ibapi.client.EClient.__init__(self, wrapper)

    def resolveContractIB(self, contractIB, reqId = DEFAULT_GET_CONTRACT_ID, maxWaitSecs = 20):
        contractDetailsQueue = finishableQueue(self.ContractDetailsInit(reqId))
        self.reqContractDetails(reqId, contractIB)
        contractDetailsResponseList = contractDetailsQueue.get(timeout = maxWaitSecs)

        while self.wrapper.isError():
            print(self.getError())
        if contractDetailsQueue.timed_out():
            print("ContractDetails Req", reqId, "expired after", MAX_WAIT_SECONDS, "secs")
        if len(contractDetailsResponseList) == 0:
            print("Failed to get additional contract details: returning unresolved contract")
            return contractIB
        if len(contractDetailsResponseList) > 1:
            print("got multiple contracts using first one")
        return contractDetailsResponseList[0].contract

    def get_IB_historical_data(self, contractIB, durationStr = "1 Y", barSizeSetting = "1 day",
                               reqId = DEFAULT_HISTORIC_DATA_ID):

        """
        Returns historical prices for a contract, up to today
        contractIB is a Contract
        :returns list of prices in 4 tuples: Open high low close volume
        """
        ## Make a place to store the data we're going to return
        historic_data_queue = finishableQueue(self.historicalDataInit(reqId))

        # Request some historical data. Native method in EClient
        self.reqHistoricalData(
            reqId,  # reqId,
            contractIB,  # contract,
            datetime.datetime.today().strftime("%Y%m%d %H:%M:%S %Z"),  # endDateTime,
            durationStr,  # durationStr,
            barSizeSetting,  # barSizeSetting,
            "TRADES",  # whatToShow,
            1,  # useRTH,
            1,  # formatDate
            False,  # KeepUpToDate <<==== added for api 9.73.2
            []  ## chartoptions not used
        )

        ## Wait until we get a completed data, an error, or get bored waiting
        MAX_WAIT_SECONDS = 20
        print("Getting historical data from the server... could take %d seconds to complete " % MAX_WAIT_SECONDS)

        historic_data = historic_data_queue.get(timeout = MAX_WAIT_SECONDS)

        while self.wrapper.isError():
            print(self.getError())

        if historic_data_queue.timed_out():
            print("HistoricalData Req", reqId, "expired after", MAX_WAIT_SECONDS, "secs")

        self.cancelHistoricalData(reqId)
        return historic_data

class ibApiMaster(ibApiWrapper, TestClient):
    def __init__(self, ipaddress, portid, clientid):
        ibApiWrapper.__init__(self)
        TestClient.__init__(self, wrapper = self)
        self.connect(ipaddress, portid, clientid)
        thread = threading.Thread(target = self.run)
        thread.start()
        setattr(self, "_thread", thread)
        self.initError()

