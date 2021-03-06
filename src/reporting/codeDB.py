# Copyright (C) 2013-2015 Ragpicker Developers.
# This file is part of Ragpicker Malware Crawler - http://code.google.com/p/malware-crawler/

import base64
import json
import logging

from core.abstracts import Report
from core.commonutils import convertDirtyDict2ASCII
from core.commonutils import flatten_dict
from utils.codeDBobjects import VOMalwareSample

try:
    import requests
except ImportError:
    raise ImportError, 'Requests is required to run this program : http://docs.python-requests.org or sudo pip install requests'

try:
    from yapsy.IPlugin import IPlugin
except ImportError:
    raise ImportError, 'Yapsy (Yet Another Plugin System) is required to run this program : http://yapsy.sourceforge.net'

log = logging.getLogger("ReportingCodeDB")

CODE_DB_URL_ADD = "https://%s:%s/sample/add"
CODE_DB_URL_STATUS = "https://%s:%s/sample/status/json/%s"
TIME_OUT = 240
VERTRAULICH_FREIGEGEBEN = "0"

STATUS_ERROR = -1
STATUS_NOT_EXISTS = 0
STATUS_PENDING = 1
STATUS_BEING_PROCESSED = 2
STATUS_FINISHED = 3
STATUS_KLONE = 4
STATUS_FAMILY = 5
STATUS_STATE_FINISHED = "finished"
HEADERS = ""

class CodeDB(IPlugin, Report):
    
    def setConfig(self):
        self.headers = {}
        self.cfg_host = self.options.get("host")
        self.cfg_port = self.options.get("port")
        cfg_user = self.options.get("user")
        cfg_password = self.options.get("password")  
        
        if not self.cfg_host or not self.cfg_port:
            raise Exception("CodeDB REST API-Server not configurated")
        
        if cfg_user and cfg_password:
            self.headers = {"Authorization" : "Basic %s" % base64.encodestring("%s:%s" % (cfg_user, cfg_password)).replace('\n', '')}

    def run(self, results, objfile):
        self.key = "CodeDB"
        # Konfiguration setzen
        self.setConfig() 
        
        # Save file
        self.processCodeDB(results, objfile, objfile.file)
        
        # Save unpacked file
        if objfile.unpacked_file:
            self.processCodeDB(results, objfile, objfile.unpacked_file, unpacked=True)
            
        # Save included files
        if len(objfile.included_files) > 0:
            log.info("Save included files")
            for incl_file in objfile.included_files:
                self.processCodeDB(results, objfile, incl_file, extracted=True)
        
    def processCodeDB(self, results, objfile, file, unpacked=False, extracted=False):
        status = self._getFileStatus(file.get_fileSha256())
        value = status.get("value")
        processingState = status.get("processingState")
        
        # Sample schon in der CodeDB vorhanden ggf. Image und Report laden
        if value == FINISHED and processingState == PROCESSING_STATE_FINISHED:
            if self.cfg_downloadImages:
                # Sample schon vorhanden -> Bild laden
                self._saveImage(file.get_fileSha256(), self.cfg_dumpdir)
            if self.cfg_saveReports:  # Sample schon vorhanden -> Save Report
                self._saveReportInMongoDB(file.get_fileSha256())
    
        # Sample in CodeDB nicht vorhanden und nach simpler Pruefung hochladbar -> Analyse durch CodeDB
        if value == NOT_EXISTS and (unpacked or extracted or self._isFileUploadable(results)):
            # Datei hochladbar und nicht vorhanden -> hinzufuegen
            uploadStatus = self._addFile(results, objfile, file, unpacked, extracted)
            # CodeDB liefert bei erfolgreichem Upload den SHA256 Hash zurueck
            if uploadStatus and uploadStatus.get("Submitted") == file.get_fileSha256():
                # Sollen Bilder geladen werden und ist ein Bild vorhanden?
                if self.cfg_downloadImages and self._isDataLoadable(file.get_fileSha256()):
                    # Bild laden
                    self._saveImage(file.get_fileSha256(), self.cfg_dumpdir)  # Soll der Report gespeichert werden und ist nicht bereits vorhanden?
                if self.cfg_saveReports and self._isDataLoadable(file.get_fileSha256()):
                    # Report in MongoDB speichern
                    self._saveReportInMongoDB(file.get_fileSha256())
            else:
                # Falscher Status oder SHA256-Hash stimmt nicht
                raise Exception("CodeDB Uploaderror: %s" % uploadStatus)

    def _addFile(self, results, objfile, file, unpacked, extracted):
        # rawFile = open(file.temp_file, 'rb')
        log.info("_addFile: " + CODE_DB_URL_ADD % (self.cfg_host, self.cfg_port))
        voCodeDB = VOMalwareSample()
        
        try:                            
            voCodeDB.setsha256(file.get_fileSha256())
            
            if unpacked:
                # Bei entpackten Samples wird der Orighash gespeichert
                voCodeDB.setOrighash(objfile.file.get_fileSha256())
            
            voCodeDB.setVertraulich(VERTRAULICH_FREIGEGEBEN)
            voCodeDB.setFileName(convertDirtyDict2ASCII(objfile.get_url_filename()))
            
            info = results.get("Info")
            
            # Ist Datei exe, dll, sys?
            if(info.get("file").get("EXE") == True):
                voCodeDB.setbinType("exe")
            elif(info.get("file").get("DLL") == True):
                voCodeDB.setbinType("dll")
            elif(info.get("file").get("DRIVER") == True):
                voCodeDB.setbinType("sys")
                
            voCodeDB.setDownloadDatestamp(info.get("analyse").get("started").strftime("%Y-%m-%d %H:%M"))
            voCodeDB.setDownloadHostname(convertDirtyDict2ASCII(info.get("url").get("hostname")))
            
            # Felder nicht zwingend vorhanden
            if "OwnLocation" in results:    
                ownLocation = results.get('OwnLocation')
                voCodeDB.setGeolocationSelf(ownLocation.get("country"))
                
            # GeolocationHost nur vorhanden wenn InetSourceAnalysis benutzt wird
            if results.has_key('InetSourceAnalysis') and results.get('InetSourceAnalysis').has_key('URLVoid'):
                urlResult = results.get('InetSourceAnalysis').get('URLVoid').get('urlResult')
                voCodeDB.setGeolocationHost(urlResult.get('CountryCode'))
                voCodeDB.setDownloadIP(urlResult.get("IP"))                

            voCodeDB.setTags(self._getTags(results, objfile, unpacked, extracted))
            # Debug Ausgabe
            voCodeDB.prints()
            
            # Upload File to CodeDB
            uploadStatus = self._upload(convertDirtyDict2ASCII(objfile.get_url_filename()), file.file_data, voCodeDB)
        
            return uploadStatus
                
        except urllib2.URLError as e:
            raise Exception("Unable to establish connection to CodeDB REST API-Server server: %s" % e)
        except urllib2.HTTPError as e:
            raise Exception("Unable to perform HTTP request to CodeDB REST API-Server (http code=%s)" % e)        
    
    def _upload(self, url_filename, file_data, voCodeDB):
        for i in range(3): 
            # Formular erstellen  
                      
            try:
                form = voCodeDB.toMultiPartForm()
                form.add_file_data(fieldname='sample', filename=url_filename, file_data=file_data)
                request = urllib2.Request(CODE_DB_URL_ADD % (self.cfg_host, self.cfg_port), headers=self.headers)
                body = str(form)
            except UnicodeDecodeError as e:
                log.info("UnicodeDecodeError - fallback to base64")
                voCodeDB.setBase64("True")
                form = voCodeDB.toMultiPartForm()
                form.add_file_data_b64(fieldname='sample', filename=url_filename, file_data=file_data)
                request = urllib2.Request(CODE_DB_URL_ADD % (self.cfg_host, self.cfg_port), headers=self.headers)
                body = str(form)
            
            request.add_header('Content-type', form.get_content_type())
            request.add_header('Content-length', len(body))
            request.add_data(body)
            response_data = urllib2.urlopen(request, timeout=TIME_OUT).read()
            log.info("_addFile: " + str(response_data))
            uploadStatus = self.json2dic(response_data)
            
            if uploadStatus.get("Status") and ERROR_BAD_SHA256 in uploadStatus.get("Status"):
                log.warning("Bad SHA256, Fehlerhafter Upload!")
            else:
                log.info(uploadStatus)
                return uploadStatus
        
        return uploadStatus
    
    def _getTags(self, results, objfile, unpacked, extracted):
        tags = {}
        
        tags["Collector"] = "Ragpicker"
        # Analyse-UUID
        tags["Ragpicker-uuid"] = results.get("Info").get("analyse").get("uuid")
        
        if extracted:
            tags["OrigFileType"] = objfile.file.get_type()
            tags["ExtractedFrom"] = objfile.file.get_fileSha256()
        if unpacked:
            tags["OrigFileType"] = objfile.file.get_type()
            
        #clean tags
        for k in tags: 
            tags[k] = convertDirtyDict2ASCII(tags[k])
        
        log.debug(tags)
        
        return tags    

    def getFileStatus(self, sha256):
        # Status eines Samples in der CodeDB ( json(processingState=string,value=string (-1..3)) )
        #Werte: -1:error, 0:not exists, 1:pending; 2:being processed, 3:finished,(4:inaktiv), 5: Familie
        #PROCESSING_STATE_FINISHED = "finished"
        try:
            res = requests.get(CODE_DB_URL_STATUS + sha256, headers=HEADERS, verify=False)
            res.raise_for_status()
            data = json.loads(res.text)
        except Exception as e:
            raise Exception("Probleme bei der Durchfuehrung des Requests (http code=%s)" % e)
    
        if data.get("value") == STATUS_ERROR :
            log.error("CodeDB return State: %s - Value: %s" % (data.get("Status"), data.get("value")))
            raise Exception("CodeDB return State: %s - Value: %s" % (data.get("Status"), data.get("value")))
        
        log.info("FileStatus: " + str(data))
        return data         
    
    def _isFileUploadable(self, results):
        fileInfo = results.get("Info").get("file")
                
        if fileInfo.has_key("isProbablyPacked") and fileInfo.has_key("EXE") and fileInfo.has_key("DLL") \
        and fileInfo.get("isProbablyPacked") == False:
            return True
    
        return False
    
    def json2dic(self, data):
        try:
            dic = json.loads(data)
        except ValueError as e:
            raise Exception("Unable to convert response to JSON: %s" % e)
        return dic