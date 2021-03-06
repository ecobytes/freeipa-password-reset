# -*- coding: utf-8 -*-
from django.conf import settings

from ipalib import api, errors as ipaerrors
import boto3
import redis
import re
import subprocess
from random import SystemRandom
from datetime import datetime, timedelta

class TooMuchRetries(Exception):
    pass

class ValidateUserFailed(Exception):
    pass

class InvalidToken(Exception):
    pass
    
class AmazonSNSFailed(Exception):
    pass

class SetPasswordFailed(Exception):
    pass

class KerberosInitFailed(Exception):
    pass

class PasswdManager():
    def __init__(self):
        if self.__kerberos_has_ticket() is False:
            self.__kerberos_init()
        if api.isdone('finalize') is False:
            api.bootstrap_with_global_options(context='api')
            api.finalize()
        api.Backend.rpcclient.connect()
        self.redis = redis.StrictRedis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_DB)
    
    def __kerberos_has_ticket(self):
        process = subprocess.Popen(['/usr/bin/klist', '-s'], stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        process.communicate()
        if process.returncode == 0:
            return True
        else:
            return False
    
    def __kerberos_init(self):
        process = subprocess.Popen(['/usr/bin/kinit', '-k', '-t', str(settings.KEYTAB_PATH), str(settings.LDAP_USER), ], stderr=subprocess.STDOUT, stdout=subprocess.PIPE)
        process.communicate()
        if process.returncode != 0:
            raise  KerberosInitFailed("Cannot retrieve kerberos tiket.")
    
    def __set_password(self, uid, password):
        try:
            password_exp_days = api.Command.pwpolicy_show()['result']['krbmaxpwdlife'][0]
            date = (datetime.now() + timedelta(days=password_exp_days)).strftime("%Y%m%d%H%M%SZ")
            api.Command.user_mod(uid=unicode(uid), userpassword=unicode(password))
            api.Command.user_mod(uid=unicode(uid), setattr=unicode("krbPasswordExpiration={0}".format(date)))
        except Exception as e:
            raise SetPasswordFailed("Cannot update your password. {0}".format(e))
        
    def __validate_user(self, uid):
        phone_regexp = re.compile('^\+([\d]{11,11})$')
        try:
            user = api.Command.user_show(uid=unicode(uid))
        except ipaerrors.NotFound:
            raise ValidateUserFailed("User not found")
        if user['result']['nsaccountlock'] is True:
            raise ValidateUserFailed("Account is deactivated")
        if len(user['result']['telephonenumber']) == 0:
            raise ValidateUserFailed("No phone number")
        if phone_regexp.match(user['result']['telephonenumber'][0]) is None:
            raise ValidateUserFailed("Phone number in wrong format")
        
                    
    def __get_user_phone(self, uid):
        user = api.Command.user_show(uid=unicode(uid))
        return user['result']['telephonenumber'][0]
    
    def __gen_secure_token(self, length):
        token = int(''.join([ str(SystemRandom().randrange(9)) for i in range(length) ]))
        return token
        
    def __set_token(self, uid):
        if (self.redis.get("retry::send::{0}".format(uid)) is not None) and (int(self.redis.get("retry::send::{0}".format(uid))) >= settings.LIMIT_MAX_SEND):
            raise TooMuchRetries("Too much retries. Try later.")
        self.redis.incr("retry::send::{0}".format(uid))
        self.redis.expire("retry::send::{0}".format(uid), settings.LIMIT_TIME)
        token = self.__gen_secure_token(settings.TOKEN_LEN)
        self.redis.set("token::{0}".format(uid), token)
        self.redis.expire("token::{0}".format(uid), settings.TOKEN_LIFETIME)
            
        return token
    
    def __validate_token(self, uid, token):
        if (self.redis.get("retry::validate::{0}".format(uid)) is not None) and (int(self.redis.get("retry::validate::{0}".format(uid))) >= settings.LIMIT_MAX_VALIDATE_RETRY):
            raise TooMuchRetries("Too much retries. Try later.")
        self.redis.incr("retry::validate::{0}".format(uid))
        self.redis.expire("retry::validate::{0}".format(uid), settings.TOKEN_LIFETIME)
        server_token = self.redis.get("token::{0}".format(uid))
        if (server_token is not None) and (int(token) == int(server_token)):
            return True
        else:
            raise InvalidToken("You entered an incorrect code")
    
    def __invalidate_token(self, uid):
        self.redis.delete("token::{0}".format(uid))
    
    def __send_token(self, uid, token):
        phone = self.__get_user_phone(uid)
        try:
            sns = boto3.client('sns', aws_access_key_id=settings.AWS_KEY, aws_secret_access_key=settings.AWS_SECRET, region_name=settings.AWS_REGION)
            sns.publish(PhoneNumber=phone, Message=settings.AWS_MESSAGE_TEMPLATE.format(token), MessageAttributes={'AWS.SNS.SMS.SenderID': {'DataType': 'String', 'StringValue': settings.AWS_SENDER_ID}})
        except botocore.errorfactory:
            self.__invalidate_token(uid)
            raise AmazonSNSFailed("Cannot send SMS via Amazon SNS")

    def first_phase(self, uid):
        self.__validate_user(uid)
        token = self.__set_token(uid)
        self.__send_token(uid, token)
        
    def second_phase(self, uid, token, new_password):
        self.__validate_token(uid, token)
        self.__set_password(uid, new_password)
        self.__invalidate_token(uid)
        
        
