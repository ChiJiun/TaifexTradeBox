from abc import ABCMeta, abstractmethod
import zmq
import threading
import requests
import json
import os, sys

class TTBHelp():
    host = None
    zmqPort = None
    def __init__(self,host, zmqPort):
        print('Version:' + self.__ShowVersion())
        '''ATBHelper初始化'''
        self.host = host
        self.zmqPort = zmqPort
        self.__TestConn()

    def __ShowVersion(self):
        return '2026.07.08.1000'

    def __sub(self,symbols):
        try:
            context = zmq.Context()
            socket = context.socket(zmq.SUB)
            socket.connect("tcp://localhost:{}".format(self.zmqPort))
            socket.setsockopt(zmq.RCVHWM,0)
            socket.setsockopt_string(zmq.SUBSCRIBE,'TTB$Tick')
            symbols = list(map(lambda x: 'TTB$Tick${}'.format(x),symbols.split(',')))
            while True:
                topic = socket.recv()
                message = socket.recv()
                if (topic.decode('utf-8') in symbols):
                    obj = json.loads(message)
                    self.SHOWQUOTEDATA(obj)                    
        except Exception as inst:
            raise Exception("無法連線ZMQ")
        
    def __TestConn(self):
        try:
            res = requests.post('{}/api/Trade/CONNECT'.format(self.host),json={"Ver":self.__ShowVersion()})
            if(len(json.loads(res.text)['ErrMsg']) != 0):
                print(json.loads(res.text)['ErrMsg'])
                os._exit(0) 
            print("連線成功")
        except Exception as inst:
            print("無法連線至本地端-{}".format(self.host))
            import time
            time.sleep(2)
            os._exit(0) 

    def REGISTERQUOTEDATA(self,symbols):
        '''Register QuoteData'''
        job1 = threading.Thread(target = self.__sub, args = (symbols,))
        job1.start()      
          
    def NEWORDER(self,putOrd):
        '''下單'''
        res = requests.post('{}/api/Trade/NEWORDER'.format(self.host),json = putOrd)
        return json.loads(res.text)

    def CANCELORDER(self,canlOrd):
        '''取消單號'''
        res = requests.post('{}/api/Trade/CANCELORDER'.format(self.host),json = canlOrd)
        return json.loads(res.text) 

    def REPLACEPRICE(self,changePrice):
        '''改價'''
        res = requests.post('{}/api/Trade/REPLACEPRICE'.format(self.host),json = changePrice)
        return json.loads(res.text) 
    
    def REPLACEQTY(self,changeQty):
        '''改量'''
        res = requests.post('{}/api/Trade/REPLACEQTY'.format(self.host),json = changeQty)
        return json.loads(res.text)

    def QUERYRESTOREREPORT(self):
        '''查詢回報明細'''
        res = requests.post('{}/api/Trade/QUERYRESTOREREPORT'.format(self.host))
        return json.loads(res.text)

    def QUERYMARGIN(self):
        '''查詢權益數'''
        res = requests.post('{}/api/Trade/QUERYMARGIN'.format(self.host))
        return json.loads(res.text) 

    def QUERYRESTOREFILLREPORT(self):
        '''查詢庫存明細'''
        res = requests.post('{}/api/Trade/QUERYRESTOREFILLREPORT'.format(self.host))
        return json.loads(res.text)

class TTBModule(TTBHelp,metaclass=ABCMeta):
    def __init__(self, host,zmqPort):        
        super().__init__(host,zmqPort)

    def QUOTEDATA(self, symbols):
        '''輸入商品,多商品用逗號區格(不分大小寫)'''    
        super().REGISTERQUOTEDATA(symbols.upper())
    
    @abstractmethod
    def SHOWQUOTEDATA(self,obj):
        '''呈現報價(實作型)'''
