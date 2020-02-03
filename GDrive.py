from __future__ import print_function
import pickle
import os.path
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.http import MediaFileUpload

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from pathlib import Path
import json
import signal
import queue
import atexit
import time
import concurrent.futures.thread
import threading
import concurrent.futures.process
import hashlib
from errors import *
import googleapiclient.errors as apiErrors
import io
import pyrfc3339
import pytz
import datetime


class DriveClient:

    SCOPES = ['https://www.googleapis.com/auth/drive',
          'https://www.googleapis.com/auth/drive.metadata'
          ]

    fields = "nextPageToken, files(id, name, md5Checksum, mimeType, parents, modifiedTime)"

    def __init__(self, cache_dir: str):
        super().__init__()
        self.creds = None
        self.cache_dir: str = cache_dir
        self.terminate: bool = False
        self.threads: int = 8
        self.folderScanSleepTime: int = 0.5
        self.fileScanSleepTime: int = 0.5
        self.folderScanComplete: bool = False
        self.fileScanComplete: bool = False
        self.querySize: int = 100
        self.folderCount: int = 0
        self.fileCount: int = 0
        self.fileWriteQueue: queue.Queue = queue.Queue(1000)
        self.folderWriteQueue: queue.Queue = queue.Queue(1000)
        self.folderQueries: queue.Queue = queue.Queue(10000)
        self.fileQueries: queue.Queue = queue.Queue(10000)
        self.searchFoldersQueue: queue.Queue = queue.Queue(10000)
        self.searchFileQueue: queue.Queue = queue.Queue(10000)
        self.folderPageTokens: dict = dict()
        self.filePageTokens: dict = dict()
        
        
        self.threadPoolExecutor = concurrent.futures.thread.ThreadPoolExecutor(
            max_workers=16)

        self.threadnames: dict = dict()
        signal.signal(signal.SIGINT, self.__keyboardINT__)
        signal.signal(signal.SIGTERM, self.__terminate__)
        atexit.register(self.__cleanup__)

    def __terminate__(self, signal, frame):
        self.__cleanup__()
    def stop(self):
        self.__cleanup__()
    def __cleanup__(self):
        self.terminate = True        
        self.threadPoolExecutor.shutdown(True)
        try:
            while 1:
                self.searchFoldersQueue.task_done()
        except ValueError:
            pass
        try:
            while 1:
                self.searchFileQueue.task_done()
        except ValueError:
            pass
        
        print("clean up called")

    def __keyboardINT__(self, signal, frame):
        print("keyboard interrupt received")
        self.__cleanup__()

    def writeToCache(self, q: queue.Queue, cache_file_name: str = "drive_cache"):

        first: bool = True
        with Path(self.cache_dir).joinpath(cache_file_name).open(mode="w", encoding="UTF-8") as f:
            f.write("[")
            while self.terminate != True:
                if q.qsize() == 0:
                    time.sleep(0.1)
                else:
                    t: str = q.get(block=True)
                    if t == None:
                        q.task_done()
                        break
                    else:
                        if first:
                            first = False
                        else:
                            f.write(",")
                        f.write(t)
                        q.task_done()
            f.write("]")

    def getService(self, api_version: str = "v3"):
        threadName: str = threading.currentThread().getName()
        if self.creds == None:
            raise AuthenticateError("Not authenticated")
        if threadName not in self.threadnames:
            service = build('drive', api_version, credentials=self.creds)
            self.threadnames[threadName] = service
        else:
            service = self.threadnames[threadName]
        return service

    def authenticate(self, credentials: str = "credentials.json", tokens: str = "token.pickle"):
        
        # The file token.pickle stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first
        # time.
        if os.path.exists(tokens):
            with open(tokens, 'rb') as token:
                self.creds = pickle.load(token)
        # If there are no (valid) credentials available, let the user log in.
        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials, DriveClient.SCOPES)
                self.creds = flow.run_local_server(port=0)
            # Save the credentials for the next run
            with open(tokens, 'wb') as token:
                pickle.dump(self.creds, token)

    def _onFileReceived(self, id, response):
        if self.terminate == False:

            results = response.get("files", [])
            nextPage = response.get("nextPageToken", None)

            # print("received")
            # print(id, response)
            # print("pageToken", nextPage)

            if nextPage != None:
                folder_str, _a = self.filePageTokens[id]
                self.filePageTokens[id] = (folder_str, nextPage)
                self.searchFileQueue.put(id, block=False)

            elif nextPage == None and id in self.filePageTokens:
                self.filePageTokens.pop(id, "")
     
            
            self.fileCount += len(results)
            

            
            r: str = str(json.dumps(results, ensure_ascii=False))[1:-1]
            if len(r) > 0:
                self.fileWriteQueue.put(r, block=False)

    def _generateFileQuery(self):

        sq = self.searchFileQueue
        fq = self.fileQueries

        if fq.qsize() > self.querySize or (sq.qsize() < 20 and fq.qsize() > 0):
            queries: list = list()
            for _ in range(self.querySize):
                if fq.qsize() > 0:
                    try:
                        queries.append(fq.get(block=False))
                        fq.task_done()
                    except queue.Empty as empty:
                        pass
                else: 
                    break
            if len(queries) > 0:
                folder_str: str = " or ".join(queries)
                m = hashlib.md5(folder_str.encode("ascii"))
                id = m.hexdigest()
                sq.put(id, block=False)
                # self.searchFileQueue.put(id, block=False)
                self.filePageTokens[id] = (folder_str, None)
                # self.filePageTokens[id] = (folder_str, None)

    def _listFiles(self, trashed: bool = False, **kwargs):

        trash_str = "true" if trashed else "false"
        sq = self.searchFileQueue
        fq = self.fileQueries
        service = self.getService()
        retry: bool = False

        while self.terminate == False:      
            
            sq_size = sq.qsize()      
            
            try:
                if sq_size > 0:  
                    id: str = sq.get(block=False)
                    
                    folder_str, nextPageToken = self.filePageTokens[id]
                    query = "( {0} ) and trashed={1} and 'me' in owners and" \
                        "mimeType!='application/vnd.google-apps.folder'".format(
                            folder_str, trash_str)
                    l = service.files().list(
                        pageSize=1000, fields=DriveClient.fields,
                        q=query, spaces="drive", pageToken=nextPageToken
                    )
                    
                    response = l.execute()
                    self._onFileReceived(id, response)
                self._generateFileQuery()
            except queue.Empty as empty:
                
                pass
            except Exception as exc:
                
                
                if isinstance(exc, apiErrors.HttpError):
                    cause = RequestError(exc).getCause()
                    if cause == RequestError.RATE_LIMIT_EXCEEDED:
                        print("user rate limit exceeded")
                        time.sleep(3)
                        retry = True
                        self.filePageTokens[id] = (folder_str, nextPageToken)
                        sq.put(id, block=False)
                
                    sq.task_done()
                else:
                    raise
            else:
                if sq_size > 0:
                    sq.task_done()
                time.sleep(self.fileScanSleepTime)
                retry = False
                
        
        while sq.qsize() > 0:
            sq.get(block=False)
            sq.task_done()
        while fq.qsize() > 0:
            fq.get(block=False)
            fq.task_done()
        
    def _generateFolderQuery(self):

        sq = self.searchFoldersQueue
        fq = self.folderQueries

        if fq.qsize() > self.querySize or (sq.qsize() < 20 and fq.qsize() > 0):
            queries: list = list()
            for _ in range(self.querySize):
                if fq.qsize() > 0:
                    try:
                        queries.append(fq.get(block=False))
                        fq.task_done()
                    except queue.Empty as empty:
                        pass
                else: 
                    break
            if len(queries) > 0:
                folder_str: str = " or ".join(queries)
                m = hashlib.md5(folder_str.encode("ascii"))
                id = m.hexdigest()
                sq.put(id, block=False)
                # self.searchFileQueue.put(id, block=False)
                self.folderPageTokens[id] = (folder_str, None)
                # self.filePageTokens[id] = (folder_str, None)

    def _onFolderReceived(self, id, response):
        if self.terminate == False:

            results = response.get("files", [])
            nextPage = response.get("nextPageToken", None)

            # print("received")
            # print(id, response)
            # print("pageToken", nextPage)

            if nextPage != None:
                folder_str, _a = self.folderPageTokens[id]
                self.folderPageTokens[id] = (folder_str, nextPage)
                self.searchFoldersQueue.put(id, block=False)

            elif nextPage == None and id in self.folderPageTokens:
                self.folderPageTokens.pop(id, "")
     
            for f in results:

                folder: str = "'{}' in parents".format(f["id"])
                self.folderQueries.put(folder, block=False)  
                self.fileQueries.put(folder, block=False)
                self.folderCount += 1
                modifiedTime: str = f["modifiedTime"]
                newTime:str = str(pyrfc3339.parse(modifiedTime)
                .replace(tzinfo=None)) # modified time is always in UTC
                f["modifiedTime"] = newTime

            
            r: str = str(json.dumps(results, ensure_ascii=False))[1:-1]
            if len(r) > 0:
                self.folderWriteQueue.put(r, block=False)
        
    def _listFolders(self, trashed: bool = False, **kwargs):

        trash_str = "true" if trashed else "false"
        sq = self.searchFoldersQueue
        fq = self.folderQueries
        service = self.getService()
        retry: bool = False

        while self.terminate == False:      
            
            sq_size = sq.qsize()      
            
            try:
                if sq_size > 0:  
                    id: str = sq.get(block=False)
                    
                    folder_str, nextPageToken = self.folderPageTokens[id]
                    query = "( {0} ) and trashed={1} and 'me' in owners and" \
                        "mimeType='application/vnd.google-apps.folder'".format(
                            folder_str, trash_str)
                    l = service.files().list(
                        pageSize=1000, fields=DriveClient.fields,
                        q=query, spaces="drive", pageToken=nextPageToken
                    )
                    
                    response = l.execute()
                    self._onFolderReceived(id, response)
                self._generateFolderQuery()
            except queue.Empty as empty:
                
                pass
            except Exception as exc:
                
                
                if isinstance(exc, apiErrors.HttpError):
                    cause = RequestError(exc).getCause()
                    if cause == RequestError.RATE_LIMIT_EXCEEDED:
                        print("user rate limit exceeded")
                        time.sleep(3)
                        retry = True
                        self.folderPageTokens[id] = (folder_str, nextPageToken)
                        sq.put(id, block=False)
                
                    sq.task_done()
                else:
                    raise
            else:
                if sq_size > 0:
                    sq.task_done()
                time.sleep(self.folderScanSleepTime)
                retry = False
                
        
        while sq.qsize() > 0:
            sq.get(block=False)
            sq.task_done()
        while fq.qsize() > 0:
            fq.get(block=False)
            fq.task_done()

    def listAll(self, folder: str = "root", trashed: bool = False, mimeType: str = "", **kwargs):
        '''Args: folder: search under this directory, trashed: show trashed \n
        return tuple (folderCount, fileCount)
        '''
        self.threadPoolExecutor.submit(
            self.writeToCache, self.fileWriteQueue, cache_file_name="remote_cache_files")
        self.threadPoolExecutor.submit(
            self.writeToCache, self.folderWriteQueue, cache_file_name="remote_cache_folders")

        folder_str: str = "'{0}' in parents".format(folder)
        m = hashlib.md5(folder_str.encode("ascii"))
        id: str = m.hexdigest()
        self.searchFoldersQueue.put(id, block=False)
        self.searchFileQueue.put(id, block=False)
        self.folderPageTokens[id] = (folder_str, None)
        self.filePageTokens[id] = (folder_str, None)
        for i in range(self.threads):
            self.threadPoolExecutor.submit(
                    self._listFolders, folder=folder)
            self.threadPoolExecutor.submit(
                    self._listFiles, folder=folder)
        
        while self.terminate == False and (self.folderQueries.qsize() > 0 or self.searchFoldersQueue.qsize() > 0):
            time.sleep(1)
            self.searchFoldersQueue.join()
            self.searchFileQueue.join()
        self.terminate = True
        return self.folderCount, self.fileCount

    def upload(self, file):
        pass

    def download(self, fileID: str):
        service = self.getService()
        
        request = service.files().get_media(fileId=fileID)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            print("Download {}.".format( int(status.progress() * 100)))


    

class FileStruct:
    def __init__(self):
        super().__init__()

    def children(self):
        pass


if __name__ == "__main__":
    back = time.time()
    d = DriveClient("/home/kie/test/.sync_ignore")
    d.authenticate()
    # folderCount, fileCount = d.listAll("1QHfx3xUyKMvzPxqrI1AbEXadPnZjFs4Z")
    folderCount, fileCount = d.listAll("root")
    print(folderCount)
    print(fileCount)
    
    front = time.time()
    print(front-back)
    
    pass
