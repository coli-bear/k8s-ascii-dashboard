"""Reuse selection and table formatting between cursor/idle frames."""
class RowCache:
    def __init__(self):
        self.snapshot=None;self.values={}

    def get(self,key,snapshot,factory):
        if self.snapshot is not snapshot:
            self.snapshot=snapshot;self.values.clear()
        if key not in self.values:
            if len(self.values)>=16:self.values.pop(next(iter(self.values)))
            self.values[key]=factory()
        return self.values[key]


class CursorContent(list):
    def __init__(self,lines,count,body_only=False):
        super().__init__(lines)
        self.positions=[];self.selected=None;self.selected_line=None
        header=body_only;boundary=body_only
        for i,line in enumerate(self):
            if line.startswith('| SEL'):
                header=True;boundary=False
            elif line.startswith('+'):
                boundary=True
            elif line.startswith('| ') and boundary and header:
                self.positions.append(i);boundary=False
                if len(line)>2 and line[2]=='>':self.selected=len(self.positions)-1;self.selected_line=i
            elif line.startswith('| '):
                boundary=False
        self.enabled=len(self.positions)==count and count>0

    def select(self,index):
        if not self.enabled or not 0<=index<len(self.positions):return
        if self.selected is not None:
            pos=self.positions[self.selected];self[pos]=self[pos][:2]+' '+self[pos][3:]
        pos=self.positions[index];self[pos]=self[pos][:2]+'>'+self[pos][3:]
        self.selected=index;self.selected_line=pos
