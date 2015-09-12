#===============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of pyfa.
#
# pyfa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyfa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyfa.  If not, see <http://www.gnu.org/licenses/>.
#===============================================================================

import copy
import itertools
import json
import threading
from codecs import open
from xml.etree import ElementTree
from xml.dom import minidom
import gzip

import wx

import eos.db
import eos.types
import service
import config


class CharacterImportThread(threading.Thread):
    def __init__(self, paths, callback):
        threading.Thread.__init__(self)
        self.paths = paths
        self.callback = callback

    def run(self):
        paths = self.paths
        sCharacter = Character.getInstance()
        for path in paths:
            try:
                # we try to parse api XML data first
                with open(path, mode='r') as charFile:
                    sheet = service.ParseXML(charFile)
                    charID = sCharacter.new()
                    sCharacter.rename(charID, sheet.name+" (imported)")
                    sCharacter.apiUpdateCharSheet(charID, sheet.skills)
            except:
                # if it's not api XML data, try this
                # this is a horrible logic flow, but whatever
                try:
                    charFile = open(path, mode='r').read()
                    doc = minidom.parseString(charFile)
                    if doc.documentElement.tagName != "SerializableCCPCharacter":
                        raise RuntimeError("Incorrect EVEMon XML sheet")
                    name = doc.getElementsByTagName("name")[0].firstChild.nodeValue
                    skill_els = doc.getElementsByTagName("skill")
                    skills = []
                    for skill in skill_els:
                        skills.append({
                            "typeID": int(skill.getAttribute("typeID")),
                            "level": int(skill.getAttribute("level")),
                        })
                    charID = sCharacter.new()
                    sCharacter.rename(charID, name+" (EVEMon)")
                    sCharacter.apiUpdateCharSheet(charID, skills)
                except:
                    continue

        wx.CallAfter(self.callback)

class SkillBackupThread(threading.Thread):
    def __init__(self, path, saveFmt, activeFit, callback):
        threading.Thread.__init__(self)
        self.path = path
        self.saveFmt = saveFmt
        self.activeFit = activeFit
        self.callback = callback

    def run(self):
        path = self.path
        sCharacter = Character.getInstance()
        sFit = service.Fit.getInstance()
        fit = sFit.getFit(self.activeFit)
        backupData = ""
        if self.saveFmt == "xml" or self.saveFmt == "emp":
            backupData = sCharacter.exportXml()
        else:
            backupData = sCharacter.exportText()

        if self.saveFmt == "emp":
            with gzip.open(path, mode='wb') as backupFile:
                backupFile.write(backupData)
        else:
            with open(path, mode='w',encoding='utf-8') as backupFile:
                backupFile.write(backupData)

        wx.CallAfter(self.callback)

class Character(object):
    instance = None
    skillReqsDict = {}

    @classmethod
    def getInstance(cls):
        if cls.instance is None:
            cls.instance = Character()

        return cls.instance

    def exportText(self):
        data  = "Pyfa exported plan for \""+self.skillReqsDict['charname']+"\"\n"
        data += "=" * 79 + "\n"
        data += "\n"
        item = ""
        for s in self.skillReqsDict['skills']:
            if item == "" or not item == s["item"]:
                item = s["item"]
                data += "-" * 79 + "\n"
                data += "Skills required for {}:\n".format(item)
            data += "{}{}: {}\n".format("    " * s["indent"], s["skill"], int(s["level"]))
        data += "-" * 79 + "\n"

        return data

    def exportXml(self):
        root = ElementTree.Element("plan")
        root.attrib["name"] = "Pyfa exported plan for "+self.skillReqsDict['charname']
        root.attrib["revision"] = config.evemonMinVersion

        sorts = ElementTree.SubElement(root, "sorting")
        sorts.attrib["criteria"] = "None"
        sorts.attrib["order"] = "None"
        sorts.attrib["groupByPriority"] = "false"

        skillsSeen = set()

        for s in self.skillReqsDict['skills']:
            skillKey = str(s["skillID"])+"::"+s["skill"]+"::"+str(int(s["level"]))
            if skillKey in skillsSeen:
                pass   # Duplicate skills confuse EVEMon
            else:
                skillsSeen.add(skillKey)
                entry = ElementTree.SubElement(root, "entry")
                entry.attrib["skillID"] = str(s["skillID"])
                entry.attrib["skill"] = s["skill"]
                entry.attrib["level"] = str(int(s["level"]))
                entry.attrib["priority"] = "3"
                entry.attrib["type"] = "Prerequisite"
                notes = ElementTree.SubElement(entry, "notes")
                notes.text = entry.attrib["skill"]

        tree = ElementTree.ElementTree(root)
        data = ElementTree.tostring(root, 'utf-8')
        prettydata = minidom.parseString(data).toprettyxml(indent="  ")

        return prettydata

    def backupSkills(self, path, saveFmt, activeFit, callback):
        thread = SkillBackupThread(path, saveFmt, activeFit, callback)
        thread.start()

    def importCharacter(self, path, callback):
        thread = CharacterImportThread(path, callback)
        thread.start()

    def all0(self):
        return eos.types.Character.getAll0()

    def all0ID(self):
        return self.all0().ID

    def all5(self):
        return eos.types.Character.getAll5()

    def all5ID(self):
        return self.all5().ID

    def getCharacterList(self):
        baseChars = [eos.types.Character.getAll0(), eos.types.Character.getAll5()]
        sFit = service.Fit.getInstance()

        for thing in eos.db.character_session.dirty:
            if isinstance(thing, eos.types.Skill):
                if not thing.character.isDirty:
                    thing.character.dirty = True

        return map(lambda c: (c.ID, c.name if not c.isDirty else "{} *".format(c.name), c == sFit.character), eos.db.getCharacterList())

    def getCharacter(self, charID):
        char = eos.db.getCharacter(charID)
        return char

    def saveCharacter(self, charID):
        """
        Saves the edited character.

        I feel like there should be a better way to specifically tell
        SQLAlchemy to only flush and commit a specific object. But this magic
        function doesn't seem to exist, so instead we basically gather a list
        of our characters, expunge the entire session, and then add the
        edited character all by themself. After committing, we simply add our
        characters back to the session.

        Without doing this, this would save all edited characters, which is
        not something that should happen.
        """

        char = eos.db.getCharacter(charID)
        charList = eos.db.getCharacterList()

        eos.db.character_session.expunge_all()
        eos.db.character_session.add(char)
        eos.db.character_session.commit()
        char.dirty = False

        for char in charList:
            eos.db.character_session.add(char)

    def getSkillGroups(self):
        cat = eos.db.getCategory(16)
        groups = []
        for grp in cat.groups:
            if grp.published:
                groups.append((grp.ID, grp.name))
        return groups

    def getSkills(self, groupID):
        group = eos.db.getGroup(groupID)
        skills = []
        for skill in group.items:
            if skill.published == True:
                skills.append((skill.ID, skill.name))
        return skills

    def getSkillDescription(self, itemID):
        return eos.db.getItem(itemID).description

    def getGroupDescription(self, groupID):
        return eos.db.getMarketGroup(groupID).description

    def getSkillLevel(self, charID, skillID):
        skill = eos.db.getCharacter(charID).getSkill(skillID)
        return skill.level if skill.learned else "Not learned"

    def rename(self, charID, newName):
        char = eos.db.getCharacter(charID)
        char.name = newName
        self.saveCharacter(charID)

    def new(self):
        #@todo: seems setting skills on a new character doesn't trigger the dirty setting. Probably goes for character copy too
        char = eos.types.Character("New Character")
        eos.db.character_session.add(char)
        # We can flush this single character to the DB to get an ID. It will be
        # committed with the rename function
        eos.db.character_session.flush([char])
        return char.ID

    def getCharName(self, charID):
        return eos.db.getCharacter(charID).name

    def copy(self, charID):
        char = eos.db.getCharacter(charID)
        newChar = copy.deepcopy(char)
        eos.db.character_session.add(newChar)
        eos.db.character_session.flush([newChar])
        return newChar.ID

    def delete(self, charID):
        # It is easier to remove the character from the main session, so banish
        # it from the character session and include it over there.
        char = eos.db.getCharacter(charID)
        eos.db.character_session.expunge(char)
        eos.db.remove(char)

    def getApiDetails(self, charID):
        char = eos.db.getCharacter(charID)
        if char.chars is not None:
            chars = json.loads(char.chars)
        else:
            chars = None
        return (char.apiID or "", char.apiKey or "", char.defaultChar or "", chars or [])

    def apiEnabled(self, charID):
        id, key, default, _ = self.getApiDetails(charID)
        return id is not "" and key is not "" and default is not ""

    def apiCharList(self, charID, userID, apiKey):
        char = eos.db.getCharacter(charID)

        char.apiID = userID
        char.apiKey = apiKey

        api = service.EVEAPIConnection()
        auth = api.auth(keyID=userID, vCode=apiKey)
        apiResult = auth.account.Characters()
        charList = map(lambda c: unicode(c.name), apiResult.characters)

        char.chars = json.dumps(charList)
        return charList

    def apiFetch(self, charID, charName):
        dbChar = eos.db.getCharacter(charID)
        dbChar.defaultChar = charName

        api = service.EVEAPIConnection()
        auth = api.auth(keyID=dbChar.apiID, vCode=dbChar.apiKey)
        apiResult = auth.account.Characters()
        charID = None
        for char in apiResult.characters:
            if char.name == charName:
                charID = char.characterID

        if charID == None:
            return

        sheet = auth.character(charID).CharacterSheet()

        dbChar.apiUpdateCharSheet(sheet.skills)
        eos.db.commit()

    def apiUpdateCharSheet(self, charID, skills):
        char = eos.db.getCharacter(charID)
        char.apiUpdateCharSheet(skills)
        eos.db.commit()

    def changeLevel(self, charID, skillID, level):
        char = eos.db.getCharacter(charID)
        skill = char.getSkill(skillID)
        if isinstance(level, basestring) or level > 5 or level < 0:
            skill.level = None
        else:
            skill.level = level

    def addImplant(self, charID, itemID):
        char = eos.db.getCharacter(charID)
        implant = eos.types.Implant(eos.db.getItem(itemID))
        char.implants.freeSlot(implant.slot)
        char.implants.append(implant)

    def removeImplant(self, charID, slot):
        char = eos.db.getCharacter(charID)
        char.implants.freeSlot(slot)

    def getImplants(self, charID):
        char = eos.db.getCharacter(charID)
        return char.implants

    def checkRequirements(self, fit):
        toCheck = []
        reqs = {}
        for thing in itertools.chain(fit.modules, fit.drones, (fit.ship,)):
            for attr in ("item", "charge"):
                subThing = getattr(thing, attr, None)
                subReqs = {}
                if subThing is not None:
                    self._checkRequirements(fit, fit.character, subThing, subReqs)
                    if subReqs:
                        reqs[subThing] = subReqs

        return reqs

    def _checkRequirements(self, fit, char, subThing, reqs):
        for req, level in subThing.requiredSkills.iteritems():
            name = req.name
            ID = req.ID
            info = reqs.get(name)
            currLevel, subs = info if info is not None else 0, {}
            if level > currLevel and (char is None or char.getSkill(req).level < level):
                reqs[name] = (level, ID, subs)
                self._checkRequirements(fit, char, req, subs)

        return reqs
